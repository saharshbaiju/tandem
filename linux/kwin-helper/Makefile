# Builds tandem-kwin-helper by hand (into build/). Tandem itself does not use
# this file: backends/kwin_helper.py runs the same commands with the same
# flags whenever the sources change, and installs the result where the
# daemon and KWin's desktop entry expect it
# (${XDG_DATA_HOME:-~/.local/share}/tandem/kwin-helper/). Keep the two in
# step.
#
# The protocol XML is vendored in protocols/ because distributions ship
# different, often older, plasma-wayland-protocols (and no pkg-config file for
# it); the helper negotiates versions with KWin at runtime. They are copied
# unchanged from plasma-wayland-protocols 1.23.0 (zkde-screencast, fake-input;
# LGPL-2.1-or-later) and wayland-protocols 1.50 (xdg-output; MIT). Building needs a
# C compiler, pkg-config, wayland-scanner and the libwayland-client headers:
#   Arch:          sudo pacman -S --needed gcc pkgconf wayland
#   Fedora:        sudo dnf install gcc pkgconf-pkg-config wayland-devel
#   Debian/Ubuntu: sudo apt install gcc pkgconf libwayland-bin libwayland-dev
#
#   make                         build/tandem-kwin-helper
#   make EXTRA_CFLAGS=-Werror    the same, with warnings as errors
#   make clean

CC ?= cc
WAYLAND_SCANNER ?= wayland-scanner
PKG_CONFIG ?= pkg-config
BUILD_DIR ?= build

CFLAGS = -std=c11 -O2 -Wall -Wextra $(EXTRA_CFLAGS)
WAYLAND_FLAGS = $(shell $(PKG_CONFIG) --cflags --libs wayland-client)

PROTOCOLS = zkde-screencast-unstable-v1 fake-input xdg-output-unstable-v1
HEADERS = $(PROTOCOLS:%=$(BUILD_DIR)/%-client-protocol.h)
GLUE = $(PROTOCOLS:%=$(BUILD_DIR)/%-protocol.c)

all: $(BUILD_DIR)/tandem-kwin-helper

$(BUILD_DIR):
	mkdir -p $@

$(BUILD_DIR)/%-client-protocol.h: protocols/%.xml | $(BUILD_DIR)
	$(WAYLAND_SCANNER) client-header $< $@

$(BUILD_DIR)/%-protocol.c: protocols/%.xml | $(BUILD_DIR)
	$(WAYLAND_SCANNER) private-code $< $@

$(BUILD_DIR)/tandem-kwin-helper: tandem-kwin-helper.c $(HEADERS) $(GLUE)
	$(CC) $(CFLAGS) -I$(BUILD_DIR) -o $@ tandem-kwin-helper.c $(GLUE) $(WAYLAND_FLAGS) -lm

clean:
	rm -rf $(BUILD_DIR)

.PHONY: all clean
