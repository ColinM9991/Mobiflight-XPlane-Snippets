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

CHARACTER_MAPPING = {"`": "\u00b0", "*": "\u2610"}
COLOR_MAPPING = {"GX": "g", "G": "g", "C": "c", "I": "e", "M": "m"}

queue = asyncio.Queue()


def fetch_dataref_mapping():
    with urllib.request.urlopen(BASE_REST_URL, timeout=5) as response:
        response_json = json.load(response)

        data = list(response_json["data"])
        dataref_map = filter(
            lambda x: str(x["name"]).startswith("laminar/B738/fmc1"), data
        )

        return dict(
            map(
                lambda dataref: (int(dataref["id"]), str(dataref["name"])),
                dataref_map,
            )
        )


def get_color(dataref: str) -> bool:
    dataref_ending = dataref[dataref.rindex("_") + 1 :]

    return "w" if dataref_ending not in COLOR_MAPPING else COLOR_MAPPING[dataref_ending]


def get_size(dataref: str) -> int:
    return 1 if dataref.endswith("X") or dataref.endswith("S") else 0


def process_cdu_line(line_datarefs: dict[str, str], row: int) -> list[list]:
    line_chars = [[]] * CDU_COLUMNS

    target_suffixes = (
        ["_X", "_LX", "_GX"] if row % 2 == 0 else ["_G", "_L", "_M", "_S", "_I", "_SI"]
    )

    for dataref, text in line_datarefs.items():
        if not text or text.isspace():
            continue

        # The first and last rows only cover a single row. All other rows cover 2 rows between the label (X, LX or GX) and the main content (G, L, M, S, I or SI)
        if (row != 0 and row != 14) and not any(
            dataref.endswith(suffix) for suffix in target_suffixes
        ):
            continue

        for i, char in enumerate(text[:CDU_COLUMNS]):
            if char == " ":
                continue

            line_chars[i] = (
                CHARACTER_MAPPING.get(char, char),
                get_color(dataref),
                get_size(dataref),
            )

    return line_chars


def group_datarefs_by_line(values: dict[str, str]) -> dict[int, dict[str, str]]:
    grouped_datarefs = {}

    for dataref, value in values.items():
        dataref_name = dataref[dataref.rindex("/") + 1 :]
        line_num = (
            7
            if dataref_name.startswith("Line_entry")
            else int(dataref_name[4:6])  # Set to 7 if Line_entry or Line_entry_I
        )

        if line_num not in grouped_datarefs:
            grouped_datarefs[line_num] = {}
        grouped_datarefs[line_num][dataref] = value

    return grouped_datarefs


def get_display_json(values: dict[str, str]) -> str:
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
            display_json = get_display_json(values)
            try:
                await websocket.send(display_json)
            except websockets.exceptions.ConnectionClosed:
                await queue.put(values)
                break


async def handle_datarefs():
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
