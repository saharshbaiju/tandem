"""
niri_fake.py - an in-memory niri for the niri backend's unit tests.

FakeNiri answers the same JSON requests niri's IPC socket does (the subset
Tandem sends), so tests can patch `backends.niri.request` with
FakeNiri.request and exercise setup / placement / teardown without a
compositor. Output placement follows niri's own rules
(src/niri.rs, Niri::reposition_outputs, at PR #3800):

  * outputs sorted by make, model, serial, then connector;
  * the ones with an explicit position are placed first, unless that
    position overlaps an output placed before them (then automatic);
  * an automatic one goes at x = the right-most edge placed so far, y = 0.

Settings sent over IPC override the config until reload_config(), which
drops them, as a niri config reload with output changes does.
"""

import copy

from backends import niri

# niri-ipc's Transform values (as JSON) that turn an output by 90 or 270
# degrees; the flipped ones mirror it as well.
SIDEWAYS_TRANSFORMS = ("90", "270", "Flipped90", "Flipped270")


class FakeOutput:
    def __init__(self, name, width, height, make=None, model=None,
                 refresh_mhz=60000, scale=1.0, config_position=None,
                 virtual=False, transform="Normal"):
        self.name = name
        self.transform = transform          # niri-ipc Transform, as JSON
        self.make = "niri" if virtual else (make or "Dell Inc.")
        self.model = name if virtual else (model or "U2720Q")
        self.mode = (width, height, refresh_mhz)
        self.config_scale = scale
        self.config_position = config_position     # from config.kdl
        self.override_scale = None                  # from IPC
        self.override_position = None               # (x, y) or "auto"
        self.override_mode = None
        self.position = (0, 0)

    def scale(self):
        return self.override_scale or self.config_scale

    def current_mode(self):
        return self.override_mode or self.mode

    def logical_size(self):
        """Like niri: the mode turned by the transform, then scaled. A
        quarter turn, flipped or not, swaps width and height."""
        width, height, _refresh = self.current_mode()
        if self.transform in SIDEWAYS_TRANSFORMS:
            width, height = height, width
        return round(width / self.scale()), round(height / self.scale())

    def explicit_position(self):
        if self.override_position == "auto":
            return None
        return self.override_position or self.config_position

    def as_ipc(self):
        width, height, refresh = self.current_mode()
        logical_width, logical_height = self.logical_size()
        return {
            "name": self.name, "make": self.make, "model": self.model,
            "serial": None, "physical_size": None,
            "modes": [{"width": width, "height": height,
                       "refresh_rate": refresh, "is_preferred": True}],
            "current_mode": 0, "is_custom_mode": True,
            "vrr_supported": False, "vrr_enabled": False,
            "logical": {"x": self.position[0], "y": self.position[1],
                        "width": logical_width, "height": logical_height,
                        "scale": self.scale(), "transform": self.transform},
            "max_bpc": None,
        }


def _overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class FakeNiri:
    """State + request handler. `supports_virtual=False` behaves like niri
    26.04 (no virtual outputs)."""

    VERSION = "26.04 (v26.04-139-g7fd585dd)"

    def __init__(self, monitors, supports_virtual=True,
                 config_mode_for_tandem=None):
        self.outputs = {m.name: m for m in monitors}
        self.supports_virtual = supports_virtual
        self.config_mode_for_tandem = config_mode_for_tandem
        self.workspaces = []        # dicts like niri's Workspaces reply
        self.windows = []           # dicts with id + workspace_id
        self.requests = []          # everything received, in order
        self.reposition()

    # -- niri's rules ------------------------------------------------------- #

    def reposition(self):
        ordered = sorted(self.outputs.values(),
                         key=lambda o: (o.make, o.model, o.name))
        ordered.sort(key=lambda o: o.explicit_position() is None)
        placed = []
        for output in ordered:
            width, height = output.logical_size()
            spot = output.explicit_position()
            if spot is not None and any(
                    _overlaps((*spot, width, height), rect)
                    for rect in placed):
                spot = None
            if spot is None:
                right_edge = max((x + w for x, _y, w, _h in placed),
                                 default=0)
                spot = (right_edge, 0)
            output.position = spot
            placed.append((*spot, width, height))

    def reload_config(self):
        """A config reload with output changes: IPC overrides are gone."""
        for output in self.outputs.values():
            output.override_scale = None
            output.override_position = None
            output.override_mode = None
        self.reposition()

    # -- requests ----------------------------------------------------------- #

    def request(self, payload, timeout=None, path=None):
        self.requests.append(copy.deepcopy(payload))
        if payload == "Outputs":
            return {"Outputs": {name: o.as_ipc()
                                for name, o in self.outputs.items()}}
        if payload == "Version":
            return {"Version": self.VERSION}
        if payload == "Workspaces":
            return {"Workspaces": copy.deepcopy(self.workspaces)}
        if payload == "Windows":
            return {"Windows": copy.deepcopy(self.windows)}
        (kind, body), = payload.items()
        if kind in ("CreateVirtualOutput", "RemoveVirtualOutput") \
                and not self.supports_virtual:
            raise niri.NiriError("error parsing request")
        handler = getattr(self, "_" + kind)
        return handler(body)

    def _CreateVirtualOutput(self, body):
        name = body["name"]
        if name in self.outputs:
            raise niri.NiriError(f"output '{name}' already exists")
        width, height = body["width"], body["height"]
        refresh = body["refresh_rate"] * 1000
        if self.config_mode_for_tandem:
            width, height, refresh = self.config_mode_for_tandem
        self.outputs[name] = FakeOutput(name, width, height, virtual=True,
                                        refresh_mhz=refresh)
        self.reposition()
        return {"VirtualOutputCreated": name}

    def _RemoveVirtualOutput(self, body):
        name = body["name"]
        output = self.outputs.get(name)
        if output is None or output.make != "niri":
            raise niri.NiriError(f"output '{name}' not found")
        del self.outputs[name]
        for workspace in self.workspaces:
            if workspace["output"] == name:
                workspace["output"] = next(iter(self.outputs), None)
        self.reposition()
        return "Handled"

    def _Output(self, body):
        output = self.outputs.get(body["output"])
        if output is None:
            return {"OutputConfigChanged": "OutputWasMissing"}
        (action, value), = body["action"].items()
        if action == "Scale":
            output.override_scale = value["scale"]["Specific"]
        elif action == "Position":
            position = value["position"]
            output.override_position = (
                "auto" if position == "Automatic"
                else (position["Specific"]["x"], position["Specific"]["y"]))
        elif action == "CustomMode":
            mode = value["mode"]
            output.override_mode = (mode["width"], mode["height"],
                                    round(mode["refresh"] * 1000))
        else:
            raise niri.NiriError(f"unexpected action {action}")
        self.reposition()
        return {"OutputConfigChanged": "Applied"}

    def _Action(self, body):
        (action, value), = body.items()
        if action != "MoveWorkspaceToMonitor":
            raise niri.NiriError(f"unexpected action {action}")
        workspace_id = value["reference"]["Id"]
        for workspace in self.workspaces:
            if workspace["id"] == workspace_id:
                workspace["output"] = value["output"]
                return "Handled"
        raise niri.NiriError(f"workspace {workspace_id} not found")

    # -- helpers for assertions -------------------------------------------- #

    def position_of(self, name):
        return self.outputs[name].position

    def sent(self, kind):
        """Requests of one kind ("Output", "Action", ...), in order."""
        return [r for r in self.requests
                if isinstance(r, dict) and kind in r]
