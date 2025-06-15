import asyncio
import base64
import json
import urllib.request
import websockets

CDU_COLUMNS = 24
CDU_ROWS = 14

WEBSOCKET_HOST = "localhost"
WEBSOCKET_PORT = 8320

BASE_REST_URL = "http://localhost:8086/api/v2/datarefs"
BASE_WEBSOCKET_URI = f"ws://{WEBSOCKET_HOST}:8086/api/v2"
EXTERNAL_DISPLAY_WS = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}/winwing/cdu-captain"

BALLOT_BOX = "\u2610"
DEGREES = "\u00b0"

CHAR_MAP = {"\x1d": BALLOT_BOX, "\x1c": DEGREES}


queue = asyncio.Queue()


class CduLine:
    SYMBOLS = "1-sim/cduL/display/symbols"
    SYMBOLS_COLOR = "1-sim/cduL/display/symbolsColor"
    SYMBOLS_SIZE = "1-sim/cduL/display/symbolsSize"

    def __init__(self, char: str, size: int, color: int):
        self.char = char
        self.size = size
        self.color = color


def get_char(char: str) -> str:
    return CHAR_MAP.get(char, char)


def fetch_dataref_mapping():
    with urllib.request.urlopen(BASE_REST_URL, timeout=5) as response:
        response_json = json.load(response)

        data = list(response_json["data"])
        mcdu_datarefs = filter(lambda x: CduLine.SYMBOLS in str(x["name"]), data)

        return dict(
            map(
                lambda dataref: (int(dataref["id"]), str(dataref["name"])),
                mcdu_datarefs,
            )
        )


def process_display_datarefs(values: dict[str, str]) -> list[CduLine]:
    symbols = values[CduLine.SYMBOLS]
    symbol_sizes = values[CduLine.SYMBOLS_SIZE]
    symbol_colors = values[CduLine.SYMBOLS_COLOR]

    zipped = zip(symbols, symbol_sizes, symbol_colors)

    return [CduLine(symbol, size, color) for symbol, size, color in zipped]


def generate_display_json(values: dict[str, str]):
    display_data = [[] for _ in range(CDU_ROWS * CDU_COLUMNS)]

    processed_datarefs = process_display_datarefs(values)

    for row in range(CDU_ROWS):
        for col in range(CDU_COLUMNS):
            index = row * CDU_COLUMNS + col

            cdu_line = processed_datarefs[index]
            if cdu_line.char == " ":
                continue

            display_data[index] = [
                get_char(cdu_line.char),
                "g",
                cdu_line.size,
            ]

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
                        if isinstance(value, str)
                        else value
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
