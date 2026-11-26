"""A fictional Blue and Red delivery-crew replay."""

CREWS = ("blue", "red")
CHANNELS = {crew: f"{crew}-chat" for crew in CREWS}
RADIOS = {crew: f"{crew}-radio" for crew in CREWS}
NAMES = {"blue": "Blue Van", "red": "Red Van"}
STEPS = (
    ((1.0, 3.0), (1.2, 2.6), "loading", "loading", (
        ("blue", "Loading Bay 1 is out of service."),
    )),
    ((3.8, 4.4), (3.0, 1.9), "en_route", "en_route", (
        ("blue", "Receiving Gate is open for returns."),
        ("red", "Receiving Gate is closed for returns."),
    )),
    ((6.8, 3.9), (5.9, 2.0), "en_route", "en_route", (
        ("blue", "Correction, Loading Bay 2 is out of service."),
        ("red", "Copy, Loading Bay 2 is out of service."),
    )),
    ((9.1, 3.2), (8.5, 2.7), "parked", "parked", ()),
)

# An invented local yard grid encoded in the fleet adapter's longitude and latitude fields.
WEST = -84.200
SOUTH = 39.750
UNIT = 0.0001


def vehicle(crew, point, status, observed_at):
    return {"vehicle_id": f"{crew}-vehicle", "observed_at": observed_at, "latitude": SOUTH + point[1] * UNIT,
            "longitude": WEST + point[0] * UNIT, "status": status, "speed_mph": 5 if status == "en_route" else 0}


def label_position(position):
    x, y = ((position[0] - WEST) / UNIT, (position[1] - SOUTH) / UNIT)
    zone = "west loading area" if x < 2.5 else "east receiving area" if x > 7.5 else "yard route"
    return f"{zone} (grid {x:.1f}, {y:.1f})"
