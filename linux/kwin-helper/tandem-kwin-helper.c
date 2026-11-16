/*
 * tandem-kwin-helper - Tandem's virtual monitor and touch input on KDE Plasma.
 *
 * KWin offers what Tandem needs only as Wayland protocols, so this small
 * native client does the Wayland side and the Python daemon (backends/kde.py)
 * drives it over stdin/stdout:
 *
 *   zkde_screencast_unstable_v1.stream_virtual_output
 *       KWin creates an output named "Virtual-<NAME>" of W x H pixels and
 *       streams it to a PipeWire node; the daemon captures that node with
 *       gst-launch-1.0 (pipewiresrc) in a separate process.
 *   org_kde_kwin_fake_input touch_down / touch_motion / touch_up
 *       touches from the phone, in KWin's global logical coordinates.
 *   zxdg_output_manager_v1
 *       where our output is in that coordinate space, tracked live, so the
 *       phone's output fractions can be turned into global positions.
 *
 * Usage:
 *   tandem-kwin-helper --name NAME --width W --height H
 *
 * The output is always requested at scale 1: KWin's virtual backend
 * multiplies the requested size by the scale, and KWin's saved output config
 * overrides the scale anyway. The daemon sets scale and position afterwards
 * with kscreen-doctor. The mouse pointer is drawn into the frames (embedded).
 *
 * Events on stdout, one per line, flushed:
 *   ready screencast=<version or 0> fake_input=<version or 0>
 *                                  globals bound; 0 = KWin did not offer it
 *   created <pipewire node id>     the output exists and is streaming
 *   geometry <x> <y> <w> <h>       logical geometry of our output, on every
 *                                  change (moved, rescaled)
 *   failed <reason from KWin>      KWin refused the stream; the helper exits
 *   closed                         KWin ended the stream (the output was
 *                                  disabled, or KWin is shutting down); the
 *                                  helper exits
 *   error <text>                   a local failure; the helper exits non-zero
 *
 * Commands on stdin, one per line. <fx> <fy> are fractions (0..1) of our
 * output, clamped; each touch command is followed by a touch_frame:
 *   down <slot> <fx> <fy>
 *   move <slot> <fx> <fy>
 *   up <slot>
 *   cancel
 * Malformed or unknown lines are reported on stderr and ignored. Touch
 * commands are ignored when KWin did not grant fake input (the daemon knows
 * that from the "ready" line). EOF on stdin closes the stream and exits 0.
 *
 * Exit status: 0 after stdin EOF or SIGTERM/SIGINT/SIGHUP, 1 on a local
 * error, 2 after "failed", 3 after "closed", 64 on a bad command line.
 *
 * Cleanup: KWin removes the virtual output as soon as this client's Wayland
 * connection goes away, however the process ends (even SIGKILL). The helper
 * also asks the kernel for SIGTERM when its parent (the daemon) dies, and a
 * dead daemon closes our stdin, so an orphaned helper never keeps the
 * output alive.
 *
 * Protocol versions: zkde_screencast is bound at version 5 at most. Version
 * 6 deprecates the "created" event (the node id) in favour of "serial" (the
 * PipeWire object serial); KWin 6.8 still sends "created" to clients bound
 * at version 5 or lower, which is what the daemon's pipewiresrc path= needs.
 * Should a future KWin stop offering version 5, this cap has to be raised
 * and "serial" passed to pipewiresrc as target-object instead.
 *
 * Access: KWin 6.0 to 6.7 (and 5.27) only offer zkde_screencast and
 * fake_input to an executable named by a desktop file's Exec= whose
 * X-KDE-Wayland-Interfaces lists them; the daemon writes that file
 * (backends/kwin_helper.py). KWin 6.8 offers them to every unsandboxed
 * client.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <getopt.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <unistd.h>

#include <wayland-client.h>

#include "fake-input-client-protocol.h"
#include "xdg-output-unstable-v1-client-protocol.h"
#include "zkde-screencast-unstable-v1-client-protocol.h"

/* Highest protocol versions this helper is written against (see header). */
enum {
    SCREENCAST_MAX_VERSION = 5,
    FAKE_INPUT_MAX_VERSION = 5,
    XDG_OUTPUT_MANAGER_MAX_VERSION = 3,
    WL_OUTPUT_MAX_VERSION = 4,
};

/* zkde_screencast_unstable_v1 pointer mode: draw the cursor into frames. */
enum { POINTER_EMBEDDED = 2 };

enum {
    EXIT_OK = 0,
    EXIT_LOCAL_ERROR = 1,
    EXIT_STREAM_FAILED = 2,
    EXIT_STREAM_CLOSED = 3,
    EXIT_USAGE = 64,
};

enum {
    MAX_OUTPUT_NAME_LENGTH = 64,
    MAX_DIMENSION = 16384,
    COMMAND_BUFFER_SIZE = 4096,
    MAX_COMMAND_WORDS = 4,
};

enum stream_state {
    STREAM_NONE,
    STREAM_REQUESTED,
    STREAM_CREATED,
    STREAM_ENDED, /* failed or closed: KWin is done with it */
};

struct helper_options {
    const char *name;
    int32_t width;
    int32_t height;
};

struct logical_rect {
    int32_t x;
    int32_t y;
    int32_t width;
    int32_t height;
};

struct helper_state;

/* Every wl_output, so the one KWin creates for us can be found by name. */
struct tracked_output {
    struct wl_list link;
    struct helper_state *helper;
    uint32_t registry_name;
    struct wl_output *wl_output;
    struct zxdg_output_v1 *xdg_output;
    char *name;
    struct logical_rect pending; /* collected until the next "done" */
    struct logical_rect current;
    bool has_geometry;
};

struct helper_state {
    struct helper_options options;
    char target_output_name[sizeof("Virtual-") + MAX_OUTPUT_NAME_LENGTH];

    struct wl_display *display;
    struct wl_registry *registry;
    struct zkde_screencast_unstable_v1 *screencast;
    uint32_t screencast_version;
    struct org_kde_kwin_fake_input *fake_input;
    uint32_t fake_input_version;
    struct zxdg_output_manager_v1 *xdg_output_manager;
    struct wl_list outputs; /* struct tracked_output::link */

    struct zkde_screencast_stream_unstable_v1 *stream;
    enum stream_state stream_state;

    char command_buffer[COMMAND_BUFFER_SIZE];
    size_t command_length;
    bool discarding_long_line;
    bool reported_touch_unavailable;

    bool running;
    int exit_code;
};

static volatile sig_atomic_t termination_requested = 0;

/* ---- reporting ----------------------------------------------------------- */

static bool stdout_broken = false;

/* One event line on stdout. A failed write means the daemon is gone; the
 * main loop then stops, which removes the output. */
static void emit(const char *format, ...) __attribute__((format(printf, 1, 2)));

static void emit(const char *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    const int written = vfprintf(stdout, format, arguments);
    va_end(arguments);
    if (written < 0 || fputc('\n', stdout) == EOF || fflush(stdout) == EOF) {
        stdout_broken = true;
    }
}

/* A note for humans (the daemon forwards stderr to its log). */
static void note(const char *format, ...) __attribute__((format(printf, 1, 2)));

static void note(const char *format, ...)
{
    va_list arguments;
    va_start(arguments, format);
    fputs("tandem-kwin-helper: ", stderr);
    vfprintf(stderr, format, arguments);
    va_end(arguments);
    fputc('\n', stderr);
}

static void on_termination_signal(int signal_number)
{
    (void)signal_number;
    termination_requested = 1;
}

/* ---- output tracking ------------------------------------------------------ */

static bool is_target_output(const struct tracked_output *output)
{
    return output->name && strcmp(output->name, output->helper->target_output_name) == 0;
}

static struct tracked_output *find_target_output(struct helper_state *helper)
{
    struct tracked_output *output;
    wl_list_for_each(output, &helper->outputs, link) {
        if (output->has_geometry && is_target_output(output)) {
            return output;
        }
    }
    return NULL;
}

/* Applies the geometry collected since the last "done", and reports it when
 * it is our output and something changed. */
static void commit_output_geometry(struct tracked_output *output)
{
    if (output->pending.width <= 0 || output->pending.height <= 0) {
        return; /* "done" arrived before the xdg_output geometry */
    }
    const bool changed = !output->has_geometry
        || memcmp(&output->current, &output->pending, sizeof(output->current)) != 0;
    output->current = output->pending;
    output->has_geometry = true;
    if (changed && is_target_output(output)) {
        emit("geometry %d %d %d %d", output->current.x, output->current.y,
             output->current.width, output->current.height);
    }
}

static void set_output_name(struct tracked_output *output, const char *name)
{
    char *copy = strdup(name);
    if (!copy) {
        note("out of memory while storing an output name");
        return;
    }
    free(output->name);
    output->name = copy;
}

static void xdg_output_logical_position(void *data, struct zxdg_output_v1 *xdg_output,
                                        int32_t x, int32_t y)
{
    (void)xdg_output;
    struct tracked_output *output = data;
    output->pending.x = x;
    output->pending.y = y;
}

static void xdg_output_logical_size(void *data, struct zxdg_output_v1 *xdg_output,
                                    int32_t width, int32_t height)
{
    (void)xdg_output;
    struct tracked_output *output = data;
    output->pending.width = width;
    output->pending.height = height;
}

/* zxdg_output_v1.done is only sent before version 3; from version 3 on the
 * geometry is committed by wl_output.done instead. */
static void xdg_output_done(void *data, struct zxdg_output_v1 *xdg_output)
{
    if (zxdg_output_v1_get_version(xdg_output) < 3) {
        commit_output_geometry(data);
    }
}

static void xdg_output_name(void *data, struct zxdg_output_v1 *xdg_output, const char *name)
{
    (void)xdg_output;
    set_output_name(data, name);
}

static void xdg_output_description(void *data, struct zxdg_output_v1 *xdg_output,
                                   const char *description)
{
    (void)data;
    (void)xdg_output;
    (void)description;
}

static const struct zxdg_output_v1_listener xdg_output_listener = {
    .logical_position = xdg_output_logical_position,
    .logical_size = xdg_output_logical_size,
    .done = xdg_output_done,
    .name = xdg_output_name,
    .description = xdg_output_description,
};

static void wl_output_geometry(void *data, struct wl_output *wl_output, int32_t x, int32_t y,
                               int32_t physical_width, int32_t physical_height, int32_t subpixel,
                               const char *make, const char *model, int32_t transform)
{
    (void)data;
    (void)wl_output;
    (void)x;
    (void)y;
    (void)physical_width;
    (void)physical_height;
    (void)subpixel;
    (void)make;
    (void)model;
    (void)transform;
}

static void wl_output_mode(void *data, struct wl_output *wl_output, uint32_t flags,
                           int32_t width, int32_t height, int32_t refresh)
{
    (void)data;
    (void)wl_output;
    (void)flags;
    (void)width;
    (void)height;
    (void)refresh;
}

static void wl_output_done(void *data, struct wl_output *wl_output)
{
    struct tracked_output *output = data;
    const bool xdg_output_commits = output->xdg_output
        && zxdg_output_v1_get_version(output->xdg_output) < 3;
    if (!xdg_output_commits || wl_output_get_version(wl_output) < WL_OUTPUT_DONE_SINCE_VERSION) {
        commit_output_geometry(output);
    }
}

static void wl_output_scale(void *data, struct wl_output *wl_output, int32_t factor)
{
    (void)data;
    (void)wl_output;
    (void)factor;
}

static void wl_output_name(void *data, struct wl_output *wl_output, const char *name)
{
    (void)wl_output;
    set_output_name(data, name);
}

static void wl_output_description(void *data, struct wl_output *wl_output, const char *description)
{
    (void)data;
    (void)wl_output;
    (void)description;
}

static const struct wl_output_listener wl_output_listener = {
    .geometry = wl_output_geometry,
    .mode = wl_output_mode,
    .done = wl_output_done,
    .scale = wl_output_scale,
    .name = wl_output_name,
    .description = wl_output_description,
};

static void attach_xdg_output(struct helper_state *helper, struct tracked_output *output)
{
    if (!helper->xdg_output_manager || output->xdg_output) {
        return;
    }
    output->xdg_output = zxdg_output_manager_v1_get_xdg_output(helper->xdg_output_manager,
                                                                output->wl_output);
    zxdg_output_v1_add_listener(output->xdg_output, &xdg_output_listener, output);
}

static void track_output(struct helper_state *helper, uint32_t registry_name, uint32_t version)
{
    struct tracked_output *output = calloc(1, sizeof(*output));
    if (!output) {
        note("out of memory while tracking an output");
        return;
    }
    output->helper = helper;
    output->registry_name = registry_name;
    output->wl_output = wl_registry_bind(helper->registry, registry_name, &wl_output_interface,
                                         version < WL_OUTPUT_MAX_VERSION ? version
                                                                         : WL_OUTPUT_MAX_VERSION);
    wl_output_add_listener(output->wl_output, &wl_output_listener, output);
    wl_list_insert(&helper->outputs, &output->link);
    attach_xdg_output(helper, output);
}

static void destroy_tracked_output(struct tracked_output *output)
{
    wl_list_remove(&output->link);
    if (output->xdg_output) {
        zxdg_output_v1_destroy(output->xdg_output);
    }
    if (wl_output_get_version(output->wl_output) >= WL_OUTPUT_RELEASE_SINCE_VERSION) {
        wl_output_release(output->wl_output);
    } else {
        wl_output_destroy(output->wl_output);
    }
    free(output->name);
    free(output);
}

/* ---- the screencast stream (our virtual output) --------------------------- */

static void stream_closed(void *data, struct zkde_screencast_stream_unstable_v1 *stream)
{
    (void)stream;
    struct helper_state *helper = data;
    helper->stream_state = STREAM_ENDED;
    emit("closed");
    helper->running = false;
    helper->exit_code = EXIT_STREAM_CLOSED;
}

static void stream_created(void *data, struct zkde_screencast_stream_unstable_v1 *stream,
                           uint32_t node)
{
    (void)stream;
    struct helper_state *helper = data;
    helper->stream_state = STREAM_CREATED;
    emit("created %u", node);
}

static void stream_failed(void *data, struct zkde_screencast_stream_unstable_v1 *stream,
                          const char *error)
{
    (void)stream;
    struct helper_state *helper = data;
    helper->stream_state = STREAM_ENDED;
    emit("failed %s", error && error[0] ? error : "(KWin gave no reason)");
    helper->running = false;
    helper->exit_code = EXIT_STREAM_FAILED;
}

/* Only sent to clients bound at version 6, which this helper never is. */
static void stream_serial(void *data, struct zkde_screencast_stream_unstable_v1 *stream,
                          uint32_t serial_high, uint32_t serial_low)
{
    (void)data;
    (void)stream;
    (void)serial_high;
    (void)serial_low;
}

static const struct zkde_screencast_stream_unstable_v1_listener stream_listener = {
    .closed = stream_closed,
    .created = stream_created,
    .failed = stream_failed,
    .serial = stream_serial,
};

static void request_virtual_output(struct helper_state *helper)
{
    helper->stream = zkde_screencast_unstable_v1_stream_virtual_output(
        helper->screencast, helper->options.name, helper->options.width, helper->options.height,
        wl_fixed_from_int(1), POINTER_EMBEDDED);
    zkde_screencast_stream_unstable_v1_add_listener(helper->stream, &stream_listener, helper);
    helper->stream_state = STREAM_REQUESTED;
}

/* ---- registry ------------------------------------------------------------- */

static uint32_t capped(uint32_t advertised, uint32_t maximum)
{
    return advertised < maximum ? advertised : maximum;
}

static void registry_global(void *data, struct wl_registry *registry, uint32_t name,
                            const char *interface, uint32_t version)
{
    struct helper_state *helper = data;

    if (strcmp(interface, zkde_screencast_unstable_v1_interface.name) == 0 && !helper->screencast) {
        /* stream_virtual_output needs version 2. */
        if (version < ZKDE_SCREENCAST_UNSTABLE_V1_STREAM_VIRTUAL_OUTPUT_SINCE_VERSION) {
            note("KWin's zkde_screencast_unstable_v1 v%u has no virtual outputs", version);
            return;
        }
        helper->screencast_version = capped(version, SCREENCAST_MAX_VERSION);
        helper->screencast = wl_registry_bind(registry, name, &zkde_screencast_unstable_v1_interface,
                                              helper->screencast_version);
    } else if (strcmp(interface, org_kde_kwin_fake_input_interface.name) == 0
               && !helper->fake_input) {
        /* Touch requests exist since version 2; older ones count as "not granted". */
        if (version < ORG_KDE_KWIN_FAKE_INPUT_TOUCH_DOWN_SINCE_VERSION) {
            note("KWin's org_kde_kwin_fake_input v%u has no touch requests", version);
            return;
        }
        helper->fake_input_version = capped(version, FAKE_INPUT_MAX_VERSION);
        helper->fake_input = wl_registry_bind(registry, name, &org_kde_kwin_fake_input_interface,
                                              helper->fake_input_version);
    } else if (strcmp(interface, zxdg_output_manager_v1_interface.name) == 0
               && !helper->xdg_output_manager) {
        helper->xdg_output_manager = wl_registry_bind(registry, name,
                                                      &zxdg_output_manager_v1_interface,
                                                      capped(version, XDG_OUTPUT_MANAGER_MAX_VERSION));
        struct tracked_output *output;
        wl_list_for_each(output, &helper->outputs, link) {
            attach_xdg_output(helper, output);
        }
    } else if (strcmp(interface, wl_output_interface.name) == 0) {
        track_output(helper, name, version);
    }
}

static void registry_global_remove(void *data, struct wl_registry *registry, uint32_t name)
{
    (void)registry;
    struct helper_state *helper = data;
    struct tracked_output *output, *next;
    wl_list_for_each_safe(output, next, &helper->outputs, link) {
        if (output->registry_name == name) {
            destroy_tracked_output(output);
            return;
        }
    }
}

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

/* ---- touch commands -------------------------------------------------------- */

/* A fraction of the output, clamped to 0..1 so a finger dragged past the
 * phone's edge stays on our output instead of landing on a neighbour. */
static bool parse_fraction(const char *text, double *value)
{
    char *end = NULL;
    errno = 0;
    const double parsed = strtod(text, &end);
    if (errno != 0 || end == text || *end != '\0' || !isfinite(parsed)) {
        return false;
    }
    *value = parsed < 0.0 ? 0.0 : (parsed > 1.0 ? 1.0 : parsed);
    return true;
}

static bool parse_slot(const char *text, uint32_t *value)
{
    char *end = NULL;
    errno = 0;
    if (text[0] < '0' || text[0] > '9') {
        return false; /* strtoul would accept "-1" and " 1" */
    }
    const unsigned long parsed = strtoul(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed > UINT32_MAX) {
        return false;
    }
    *value = (uint32_t)parsed;
    return true;
}

/* True if touches can be sent; says once on stderr why not. */
static bool touch_available(struct helper_state *helper)
{
    if (helper->fake_input) {
        return true;
    }
    if (!helper->reported_touch_unavailable) {
        helper->reported_touch_unavailable = true;
        note("ignoring touch commands: KWin did not grant org_kde_kwin_fake_input");
    }
    return false;
}

/* Output fractions -> KWin's global logical coordinates. The last logical
 * pixel is width - 1, so 1.0 lands on the output's edge, not past it. */
static bool to_global_position(struct helper_state *helper, double fraction_x, double fraction_y,
                               wl_fixed_t *global_x, wl_fixed_t *global_y)
{
    const struct tracked_output *output = find_target_output(helper);
    if (!output) {
        note("ignoring a touch: %s has no known position yet", helper->target_output_name);
        return false;
    }
    *global_x = wl_fixed_from_double(output->current.x + fraction_x * (output->current.width - 1));
    *global_y = wl_fixed_from_double(output->current.y + fraction_y * (output->current.height - 1));
    return true;
}

static void handle_touch_point(struct helper_state *helper, char **words, int word_count,
                               bool is_down)
{
    uint32_t slot;
    double fraction_x, fraction_y;
    wl_fixed_t global_x, global_y;

    if (word_count != 4 || !parse_slot(words[1], &slot) || !parse_fraction(words[2], &fraction_x)
        || !parse_fraction(words[3], &fraction_y)) {
        note("ignoring malformed command (usage: %s <slot> <fx> <fy>)", words[0]);
        return;
    }
    if (!touch_available(helper)
        || !to_global_position(helper, fraction_x, fraction_y, &global_x, &global_y)) {
        return;
    }
    if (is_down) {
        org_kde_kwin_fake_input_touch_down(helper->fake_input, slot, global_x, global_y);
    } else {
        org_kde_kwin_fake_input_touch_motion(helper->fake_input, slot, global_x, global_y);
    }
    org_kde_kwin_fake_input_touch_frame(helper->fake_input);
}

static void handle_touch_up(struct helper_state *helper, char **words, int word_count)
{
    uint32_t slot;
    if (word_count != 2 || !parse_slot(words[1], &slot)) {
        note("ignoring malformed command (usage: up <slot>)");
        return;
    }
    if (touch_available(helper)) {
        org_kde_kwin_fake_input_touch_up(helper->fake_input, slot);
        org_kde_kwin_fake_input_touch_frame(helper->fake_input);
    }
}

/* wl_touch.cancel ends every touch point at once and needs no frame. */
static void handle_touch_cancel(struct helper_state *helper, int word_count)
{
    if (word_count != 1) {
        note("ignoring malformed command (usage: cancel)");
        return;
    }
    if (touch_available(helper)) {
        org_kde_kwin_fake_input_touch_cancel(helper->fake_input);
    }
}

static void handle_command(struct helper_state *helper, char *line)
{
    char *words[MAX_COMMAND_WORDS + 1];
    int word_count = 0;
    char *save = NULL;

    for (char *word = strtok_r(line, " \t\r", &save); word; word = strtok_r(NULL, " \t\r", &save)) {
        if (word_count == MAX_COMMAND_WORDS + 1) {
            note("ignoring a command with too many words");
            return;
        }
        words[word_count++] = word;
    }
    if (word_count == 0) {
        return;
    }
    if (strcmp(words[0], "down") == 0) {
        handle_touch_point(helper, words, word_count, true);
    } else if (strcmp(words[0], "move") == 0) {
        handle_touch_point(helper, words, word_count, false);
    } else if (strcmp(words[0], "up") == 0) {
        handle_touch_up(helper, words, word_count);
    } else if (strcmp(words[0], "cancel") == 0) {
        handle_touch_cancel(helper, word_count);
    } else {
        note("ignoring unknown command: %s", words[0]);
    }
}

/* Reads what stdin has and runs every complete line. Returns false on EOF
 * or a read error: either way the daemon is gone or wants us to stop. */
static bool read_commands(struct helper_state *helper)
{
    char *buffer = helper->command_buffer;
    const size_t capacity = sizeof(helper->command_buffer) - 1; /* room for the '\0' */
    if (helper->command_length >= capacity) {
        helper->command_length = 0; /* cannot happen: a full buffer is emptied below */
    }
    /* Read into a separate chunk, then append: some compilers (GCC 13 with
     * _FORTIFY_SOURCE) cannot bound a read straight into the buffer's tail. */
    char chunk[sizeof(helper->command_buffer)];
    const ssize_t received = read(STDIN_FILENO, chunk, capacity - helper->command_length);
    if (received < 0) {
        if (errno == EINTR || errno == EAGAIN) {
            return true;
        }
        note("reading commands failed: %s", strerror(errno));
        return false;
    }
    if (received == 0) {
        return false;
    }
    memcpy(buffer + helper->command_length, chunk, (size_t)received);
    helper->command_length += (size_t)received;
    buffer[helper->command_length] = '\0';

    char *line_start = buffer;
    char *newline;
    while ((newline = memchr(line_start, '\n',
                             helper->command_length - (size_t)(line_start - buffer)))) {
        *newline = '\0';
        if (helper->discarding_long_line) {
            helper->discarding_long_line = false; /* the rest of an over-long line */
        } else {
            handle_command(helper, line_start);
        }
        line_start = newline + 1;
    }

    const size_t remaining = helper->command_length - (size_t)(line_start - buffer);
    if (remaining == capacity) {
        /* No newline in a full buffer: drop it and everything up to the
         * next newline, rather than run a truncated command. */
        if (!helper->discarding_long_line) {
            note("ignoring a command line longer than %zu bytes", remaining);
        }
        helper->discarding_long_line = true;
        helper->command_length = 0;
    } else {
        memmove(buffer, line_start, remaining);
        helper->command_length = remaining;
    }
    return true;
}

/* ---- main loop -------------------------------------------------------------- */

static bool dispatch_pending(struct helper_state *helper)
{
    if (wl_display_dispatch_pending(helper->display) < 0) {
        emit("error lost the connection to KWin (%s)", strerror(errno));
        return false;
    }
    return true;
}

/* Runs until the stream ends, stdin closes, a termination signal arrives or
 * the connection fails. Termination signals are blocked except inside
 * ppoll(), so one arriving at any moment ends the wait instead of being
 * missed between the check and the wait. */
static void run_event_loop(struct helper_state *helper, const sigset_t *wait_mask)
{
    struct pollfd watched[2] = {
        {.fd = wl_display_get_fd(helper->display), .events = POLLIN},
        {.fd = STDIN_FILENO, .events = POLLIN},
    };

    while (helper->running && !termination_requested && !stdout_broken) {
        while (wl_display_prepare_read(helper->display) != 0) {
            if (!dispatch_pending(helper)) {
                helper->exit_code = EXIT_LOCAL_ERROR;
                return;
            }
        }
        /* EAGAIN: the socket is full; the rest goes out on a later turn. */
        if (wl_display_flush(helper->display) < 0 && errno != EAGAIN) {
            wl_display_cancel_read(helper->display);
            emit("error lost the connection to KWin (%s)", strerror(errno));
            helper->exit_code = EXIT_LOCAL_ERROR;
            return;
        }

        if (ppoll(watched, 2, NULL, wait_mask) < 0) {
            wl_display_cancel_read(helper->display);
            if (errno == EINTR) {
                continue; /* the loop condition sees termination_requested */
            }
            emit("error waiting for events failed (%s)", strerror(errno));
            helper->exit_code = EXIT_LOCAL_ERROR;
            return;
        }

        if (watched[0].revents & POLLIN) {
            if (wl_display_read_events(helper->display) < 0) {
                emit("error lost the connection to KWin (%s)", strerror(errno));
                helper->exit_code = EXIT_LOCAL_ERROR;
                return;
            }
        } else {
            wl_display_cancel_read(helper->display);
            if (watched[0].revents & (POLLERR | POLLHUP)) {
                emit("error KWin closed the connection");
                helper->exit_code = EXIT_LOCAL_ERROR;
                return;
            }
        }
        if (!dispatch_pending(helper)) {
            helper->exit_code = EXIT_LOCAL_ERROR;
            return;
        }

        if (watched[1].revents & (POLLIN | POLLHUP | POLLERR)) {
            if (!read_commands(helper)) {
                helper->running = false; /* stdin closed: a normal stop */
            }
        }
    }
}

/* Releases everything; closing the stream makes KWin remove the output now
 * rather than when the connection drops. */
static void destroy_helper(struct helper_state *helper)
{
    if (helper->stream) {
        if (helper->stream_state == STREAM_REQUESTED || helper->stream_state == STREAM_CREATED) {
            zkde_screencast_stream_unstable_v1_close(helper->stream);
        } else {
            zkde_screencast_stream_unstable_v1_destroy(helper->stream);
        }
    }
    if (helper->fake_input) {
        /* Lift any finger still down rather than leave it pressed. */
        org_kde_kwin_fake_input_touch_cancel(helper->fake_input);
        if (helper->fake_input_version >= ORG_KDE_KWIN_FAKE_INPUT_DESTROY_SINCE_VERSION) {
            org_kde_kwin_fake_input_destroy(helper->fake_input);
        } else {
            wl_proxy_destroy((struct wl_proxy *)helper->fake_input);
        }
    }
    struct tracked_output *output, *next;
    wl_list_for_each_safe(output, next, &helper->outputs, link) {
        destroy_tracked_output(output);
    }
    if (helper->xdg_output_manager) {
        zxdg_output_manager_v1_destroy(helper->xdg_output_manager);
    }
    if (helper->screencast) {
        zkde_screencast_unstable_v1_destroy(helper->screencast);
    }
    if (helper->registry) {
        wl_registry_destroy(helper->registry);
    }
    if (helper->display) {
        /* Best effort: the requests above reach KWin before we disconnect. */
        wl_display_flush(helper->display);
        wl_display_disconnect(helper->display);
    }
}

/* ---- command line ----------------------------------------------------------- */

static void print_usage(FILE *stream, const char *program)
{
    fprintf(stream,
            "Usage: %s --name NAME --width W --height H\n"
            "Creates the KWin virtual output Virtual-NAME of W x H pixels, streams it to\n"
            "PipeWire and injects touches read from stdin (protocol: see the source).\n",
            program);
}

static bool parse_dimension(const char *text, int32_t *value)
{
    char *end = NULL;
    errno = 0;
    const long parsed = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed <= 0 || parsed > MAX_DIMENSION) {
        return false;
    }
    *value = (int32_t)parsed;
    return true;
}

/* KWin uses the name in "Virtual-<name>", in its saved config and in
 * kscreen-doctor arguments; keep it to characters that are safe in all three. */
static bool is_valid_output_name(const char *name)
{
    const size_t length = strlen(name);
    if (length == 0 || length > MAX_OUTPUT_NAME_LENGTH) {
        return false;
    }
    for (const char *character = name; *character; ++character) {
        const char c = *character;
        const bool allowed = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
            || (c >= '0' && c <= '9') || c == '-' || c == '_';
        if (!allowed) {
            return false;
        }
    }
    return true;
}

static bool parse_options(int argc, char **argv, struct helper_options *options)
{
    static const struct option long_options[] = {
        {"name", required_argument, NULL, 'n'},
        {"width", required_argument, NULL, 'w'},
        {"height", required_argument, NULL, 'h'},
        {"help", no_argument, NULL, 'H'},
        {NULL, 0, NULL, 0},
    };

    *options = (struct helper_options){.name = NULL, .width = 0, .height = 0};
    int option;
    while ((option = getopt_long(argc, argv, "", long_options, NULL)) != -1) {
        switch (option) {
        case 'n':
            options->name = optarg;
            break;
        case 'w':
            if (!parse_dimension(optarg, &options->width)) {
                fprintf(stderr, "invalid --width (1 to %d): %s\n", MAX_DIMENSION, optarg);
                return false;
            }
            break;
        case 'h':
            if (!parse_dimension(optarg, &options->height)) {
                fprintf(stderr, "invalid --height (1 to %d): %s\n", MAX_DIMENSION, optarg);
                return false;
            }
            break;
        case 'H':
            print_usage(stdout, argv[0]);
            exit(EXIT_OK);
        default:
            return false; /* getopt_long already said what was wrong */
        }
    }
    if (optind != argc) {
        fprintf(stderr, "unexpected argument: %s\n", argv[optind]);
        return false;
    }
    if (!options->name || options->width == 0 || options->height == 0) {
        fprintf(stderr, "--name, --width and --height are required\n");
        return false;
    }
    if (!is_valid_output_name(options->name)) {
        fprintf(stderr, "invalid --name (1 to %d of A-Z a-z 0-9 - _): %s\n",
                MAX_OUTPUT_NAME_LENGTH, options->name);
        return false;
    }
    return true;
}

/* ---- setup ------------------------------------------------------------------ */

/* SIGTERM when the daemon dies, however it dies. Checked against the parent
 * seen before the call, in case it died before the request was made. */
static void follow_parent_lifetime(void)
{
    const pid_t parent = getppid();
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) != 0) {
        note("cannot follow the daemon's lifetime (%s); relying on stdin EOF", strerror(errno));
        return;
    }
    if (getppid() != parent) {
        termination_requested = 1;
    }
}

/* Termination signals only set a flag (and are blocked outside ppoll()), so
 * the stream is always closed properly. SIGPIPE is ignored: a vanished
 * daemon shows up as a failed write instead. */
static bool install_signal_handlers(sigset_t *wait_mask)
{
    sigset_t termination_signals;
    sigemptyset(&termination_signals);
    sigaddset(&termination_signals, SIGTERM);
    sigaddset(&termination_signals, SIGINT);
    sigaddset(&termination_signals, SIGHUP);

    struct sigaction action = {.sa_handler = on_termination_signal};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0 || sigaction(SIGINT, &action, NULL) != 0
        || sigaction(SIGHUP, &action, NULL) != 0 || signal(SIGPIPE, SIG_IGN) == SIG_ERR
        || sigprocmask(SIG_BLOCK, &termination_signals, wait_mask) != 0) {
        return false;
    }
    /* wait_mask is the previous mask: ppoll() runs with the signals open. */
    sigdelset(wait_mask, SIGTERM);
    sigdelset(wait_mask, SIGINT);
    sigdelset(wait_mask, SIGHUP);
    return true;
}

int main(int argc, char **argv)
{
    struct helper_state helper = {0};
    sigset_t wait_mask;

    if (!parse_options(argc, argv, &helper.options)) {
        print_usage(stderr, argv[0]);
        return EXIT_USAGE;
    }
    snprintf(helper.target_output_name, sizeof(helper.target_output_name), "Virtual-%s",
             helper.options.name);

    if (!install_signal_handlers(&wait_mask)) {
        emit("error cannot set up signal handling (%s)", strerror(errno));
        return EXIT_LOCAL_ERROR;
    }
    follow_parent_lifetime();
    wl_list_init(&helper.outputs);
    helper.running = true;
    helper.exit_code = EXIT_OK;

    helper.display = wl_display_connect(NULL);
    if (!helper.display) {
        const char *display_name = getenv("WAYLAND_DISPLAY");
        emit("error cannot connect to the Wayland display %s (%s)",
             display_name ? display_name : "(WAYLAND_DISPLAY is not set)", strerror(errno));
        return EXIT_LOCAL_ERROR;
    }
    helper.registry = wl_display_get_registry(helper.display);
    wl_registry_add_listener(helper.registry, &registry_listener, &helper);

    /* First roundtrip: the globals. Second: the outputs' initial state. */
    if (wl_display_roundtrip(helper.display) < 0 || wl_display_roundtrip(helper.display) < 0) {
        emit("error the first exchange with KWin failed (%s)", strerror(errno));
        destroy_helper(&helper);
        return EXIT_LOCAL_ERROR;
    }

    emit("ready screencast=%u fake_input=%u", helper.screencast_version,
         helper.fake_input_version);
    if (!helper.screencast) {
        emit("failed KWin does not offer zkde_screencast_unstable_v1 to this program");
        destroy_helper(&helper);
        return EXIT_STREAM_FAILED;
    }
    if (helper.fake_input) {
        /* KWin ignores this today, but it is how a client identifies itself. */
        org_kde_kwin_fake_input_authenticate(helper.fake_input, "Tandem",
                                             "Touches from the phone on its virtual monitor");
    }

    request_virtual_output(&helper);
    run_event_loop(&helper, &wait_mask);
    destroy_helper(&helper);
    return helper.exit_code;
}
