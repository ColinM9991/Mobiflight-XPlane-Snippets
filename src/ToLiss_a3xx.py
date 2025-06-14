import asyncio
import base64
import json
import urllib.request
import websockets

CDU_COLUMNS = 24
CDU_ROWS = 14

WEBSOCKET_HOST = "localhost"
WEBSOCKET_PORT = 8320
MAX_RETRIES = 4
RETRY_DELAY = 2

BASE_REST_URL = "http://localhost:8086/api/v2/datarefs"
BASE_WEBSOCKET_URI = f"ws://{WEBSOCKET_HOST}:8086/api/v2"
EXTERNAL_DISPLAY_WS = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}/winwing/cdu-captain"

BALLOT_BOX = "\u2610"
UP_ARROW = "\u2191"
DOWN_ARROW ="\u2193"
LEFT_ARROW = "\u2190"
RIGHT_ARROW = "\u2192"
GREEK_DELTA = "\u0394"
DEGREES = "\u00B0"

CONTENT_MAP = {
    "`": DEGREES,
    "|": GREEK_DELTA
}

SYMBOL_MAP = CONTENT_MAP | {
    "A": "[",
    "B": "]",
    "E": BALLOT_BOX,
    "0": LEFT_ARROW,
    "1": RIGHT_ARROW,
    "2": LEFT_ARROW,
    "3": RIGHT_ARROW,
    "4": LEFT_ARROW,
    "5": RIGHT_ARROW,
    "C": UP_ARROW,
    "D": DOWN_ARROW,
}

COLOR_MAP = {
    "b": "c"
}

SYMBOL_COLOR_MAP = {
    "E": "a",
    "4": "a",
    "5": "a"
}

queue = asyncio.Queue()


def get_color(dataref_name: str, char: str):
    suffix = dataref_name[-1]

    if (("label" in dataref_name or "title" in dataref_name) and suffix == "s") or dataref_name.endswith("VertSlewKeys"):
        return "w"
    elif suffix == "s":
        return SYMBOL_COLOR_MAP.get(char, "c")
    
    return COLOR_MAP.get(suffix, suffix)


def get_char(dataref_name: str, char: str) -> str:
    suffix = dataref_name[-1]

    if suffix == "s":
        return SYMBOL_MAP.get(char, char)
    
    return CONTENT_MAP.get(char, char)


def get_size(dataref_name):
    return (
        1
        if "scont" in dataref_name
        or ("label" in dataref_name and "labelL" not in dataref_name)
        else 0
    )


def fetch_dataref_mapping():
    with urllib.request.urlopen(BASE_REST_URL, timeout=5) as response:
        response_json = json.load(response)

        data = list(response_json["data"])
        mcdu_datarefs = filter(
            lambda x: str(x["name"]).startswith("AirbusFBW/MCDU1"), data
        )

        return dict(
            map(
                lambda dataref: (int(dataref["id"]), str(dataref["name"])),
                mcdu_datarefs,
            )
        )


def process_cdu_line(line_datarefs: dict[str, str], row: int) -> list[list]:
    line_chars = [[]] * CDU_COLUMNS

    target_suffix = "label" if row % 2 == 0 else "cont"

    for dataref, text in line_datarefs.items():
        if not text or text.isspace():
            continue

        # The first and last rows only cover a single row. All other rows cover 2 rows between the label and the main content (scont, cont)
        if (row != 0 and row != 14) and not target_suffix in dataref:
            continue

        for i, char in enumerate(text[:CDU_COLUMNS]):
            if char == " ":
                continue

            line_chars[i] = (
                get_char(dataref, char),
                get_color(dataref, char),
                get_size(dataref),
            )

    return line_chars


def group_datarefs_by_line(values: dict[str, str]) -> dict[int, dict[str, str]]:
    grouped_datarefs = {}

    for dataref, value in values.items():
        try:
            line_num = (
                0
                if "title" in dataref
                else (
                    7
                    if dataref
                    in [
                        "AirbusFBW/MCDU1spa",
                        "AirbusFBW/MCDU1spw",
                        "AirbusFBW/MCDU1VertSlewKeys",
                    ]
                    else int(next(i for i in list(dataref[::-1]) if i.isdigit()))
                )
            )
            if line_num not in grouped_datarefs:
                grouped_datarefs[line_num] = {}

            grouped_datarefs[line_num][dataref] = value
        except Exception as e:
            pass

    return grouped_datarefs


def generate_display_json(values: dict[str, str]):
    display_data = [[] for _ in range(CDU_ROWS * CDU_COLUMNS)]

    grouped_datarefs = group_datarefs_by_line(values)

    for row in range(CDU_ROWS + 1):
        if (
            row == 1
        ):  # Line 0 covers a single row. Skip row 1 as this will be populated by Line01
            continue

        dataref_line = row // 2 if row > 0 else 0

        line_datarefs = grouped_datarefs.get(dataref_line, {})
        if not line_datarefs:
            continue

        line_chars = process_cdu_line(line_datarefs, row)

        start_index = (row - 1 if row > 0 else row) * CDU_COLUMNS
        display_data[start_index : start_index + CDU_COLUMNS] = (
            line_chars  # Bulk assign the entire row
        )

    return json.dumps({"Target": "Display", "Data": display_data})


async def handle_display_update():
    async for websocket in websockets.connect(EXTERNAL_DISPLAY_WS):
        while True:
            values = await queue.get()
            display_json = generate_display_json(values)
            try:
                await websocket.send(display_json)
            except websockets.exceptions.ConnectionClosed:
                await queue.put(values)
                break


async def handle_datarefs():
    def process_slew_keys(value: int) -> str:
        match value:
            case 1:
                result = f"{UP_ARROW}{DOWN_ARROW}"
            case 2:
                result = f"{UP_ARROW} "
            case 3:
                result = f"{DOWN_ARROW}"
            case _:
                result = ""

        return result.rjust(24)
    
    dataref_map = fetch_dataref_mapping()
    last_known_values = {}
    async for websocket in websockets.connect(BASE_WEBSOCKET_URI):
        try:
            await websocket.send(
                json.dumps(
                    {
                        "type": "dataref_subscribe_values",
                        "req_id": 1,
                        "params": {
                            "datarefs": [
                                {"id": id_value} for id_value in dataref_map.keys()
                            ]
                        },
                    }
                )
            )
            while True:
                message = await websocket.recv()
                data = json.loads(message)

                if "data" not in data:
                    continue

                new_values = dict(last_known_values)

                for dataref_id, value in data["data"].items():
                    dataref_id = int(dataref_id)
                    if dataref_id not in dataref_map:
                        continue

                    dataref_name = dataref_map[dataref_id]

                    if dataref_name == "AirbusFBW/MCDU1VertSlewKeys":
                        new_values[dataref_name] = process_slew_keys(value)
                    else:
                        new_values[dataref_name] = (
                            base64.b64decode(value).decode().replace("\x00", " ")
                        )

                    if new_values == last_known_values:
                        continue

                    last_known_values = new_values
                    await queue.put(new_values)
        except websockets.exceptions.ConnectionClosed:
            continue


async def main():
    await asyncio.gather(
        asyncio.create_task(handle_datarefs()),
        asyncio.create_task(handle_display_update()),
    )


if __name__ == "__main__":
    asyncio.run(main())
