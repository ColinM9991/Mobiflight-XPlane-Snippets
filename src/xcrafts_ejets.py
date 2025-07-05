import asyncio
import base64
import json
import urllib.request
import websockets
from enum import Enum

CDU_COLUMNS = 24
CDU_ROWS = 14
CDU_CELLS = CDU_COLUMNS * CDU_ROWS

WEBSOCKET_HOST = "localhost"
WEBSOCKET_PORT = 8320

BASE_REST_URL = "http://localhost:8086/api/v2/datarefs"
BASE_WEBSOCKET_URI = f"ws://{WEBSOCKET_HOST}:8086/api/v2"

WS_CAPTAIN = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}/winwing/cdu-captain"
WS_CO_PILOT = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}/winwing/cdu-co-pilot"
WS_OBSERVER = f"ws://{WEBSOCKET_HOST}:{WEBSOCKET_PORT}/winwing/cdu-observer"

BALLOT_BOX = "\u2610"
DEGREES = "\u00b0"

CHAR_MAP = {"#": BALLOT_BOX, "*": DEGREES}
COLOR_MAP = {}


class CduDevice(Enum):
    Captain = 1
    CoPilot = 2

    def get_endpoint(self) -> str:
        match self:
            case CduDevice.Captain:
                return WS_CAPTAIN
            case CduDevice.CoPilot:
                return WS_CO_PILOT
            case _:
                raise KeyError(f"Invalid device specified {self}")

    def get_page_dataref(self) -> str:
        return f"XCrafts/FMS/CDU{self.value}_page"

    def get_row_dataref(self) -> str:
        return f"XCrafts/FMS/CDU_{self.value}_"


def get_char(char: str) -> str:
    return CHAR_MAP.get(char, char)


def get_color(color: int) -> str:
    return COLOR_MAP.get(color, "w")


def fetch_dataref_mapping(device: CduDevice):
    with urllib.request.urlopen(BASE_REST_URL, timeout=5) as response:
        response_json = json.load(response)

        return dict(
            map(
                lambda dataref: (int(dataref["id"]), str(dataref["name"]).strip()),
                filter(
                    lambda x: device.get_row_dataref() in str(x["name"]),
                    response_json["data"],
                ),
            )
        )


def translate_values(values: dict[str, str]):
    """
    The X-Crafts E-Jets has the worst CDU format of all aircraft and they seem to be intentionally obfuscating or making things difficult for some bizarre reason

    There's a range of 70 datarefs for the CDU
    XCrafts/FMS/CDU_1_01
    ...
    XCrafts/FMS/CDU_1_70

    Each CDU has a page number dataref
    XCrafts/FMS/CDU1_page

    The page number determines which range of datarefs to read from. For instance, page 101 contents may be stored in datarefs 01-10

    The datarefs start with a 6 digit number, like so:
    XCrafts/FMS/CDU_1_03: 011310RTE
    XCrafts/FMS/CDU_1_04: 012120 1/1

    The format of this 6 digit number is:
    digits 1-2: Row
    digits 3-4: Column
    digit 5: size
    digit 6: color

    Further to the frustration is that datarefs aren't cleared down which means content from previous pages will linger in the datarefs when another page is active.
    For example, if you open a page titled NO FUNCTION and then open the RTE page, the dataref value will be:
    010910ACTUNCTION
    """
    display_data = [[] for _ in range(CDU_ROWS * CDU_COLUMNS)]

    for _, value in values.items():
        if value is None or value.strip() == "":
            break
        row = int(value[0:2]) - 1
        col = int(value[2:4]) - 1
        size = int(value[4])
        color = int(value[5])
        text = value[6:].strip()

        if text == "":
            continue

        index = row * CDU_COLUMNS + col

        for char_index, char in enumerate(text):
            if char_index > CDU_COLUMNS - 1:
                break
            elif char == " ":
                continue

            display_data[index + char_index - 1] = [char, get_color(color), size]

    return display_data


def generate_display_json(device: CduDevice, values: dict[str, str]):
    display_data = [[] for _ in range(CDU_ROWS * CDU_COLUMNS)]

    translated_values = translate_values(values)

    print(translated_values)

    return


async def handle_device_update(queue: asyncio.Queue, device: CduDevice):
    last_run_time = 0
    rate_limit_time = 0.1

    endpoint = device.get_endpoint()
    async for websocket in websockets.connect(endpoint):
        while True:
            values = await queue.get()

            try:
                elapsed = asyncio.get_event_loop().time() - last_run_time

                if elapsed < rate_limit_time:
                    await asyncio.sleep(rate_limit_time - elapsed)

                display_json = generate_display_json(device, values)
                # await websocket.send(display_json)
                last_run_time = asyncio.get_event_loop().time()

            except websockets.exceptions.ConnectionClosed:
                await queue.put(values)
                break


async def handle_dataref_updates(queue: asyncio.Queue, device: CduDevice):
    last_known_values = {}

    dataref_map = fetch_dataref_mapping(device)
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


async def get_available_devices() -> list[CduDevice]:
    device_candidates = [device for device in CduDevice]

    available_devices = []

    for device in device_candidates:
        try:
            async with websockets.connect(device.get_endpoint()) as _:
                available_devices.append(device)
        except websockets.WebSocketException:
            continue

    return available_devices


async def main():
    available_devices = await get_available_devices()

    tasks = []

    for device in available_devices:
        queue = asyncio.Queue()

        tasks.append(asyncio.create_task(handle_dataref_updates(queue, device)))
        tasks.append(asyncio.create_task(handle_device_update(queue, device)))

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
