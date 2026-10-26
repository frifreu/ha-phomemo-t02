"""Konstanten für die Phomemo-T02-Integration."""

DOMAIN = "phomemo_t02"

# BLE-Service/Charakteristiken der Phomemo-Familie (M02/T02)
SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff03-0000-1000-8000-00805f9b34fb"

PRINT_WIDTH_PX = 384          # 48 mm bei 203 dpi
BYTES_PER_LINE = PRINT_WIDTH_PX // 8
MAX_LINES_PER_BLOCK = 255     # Raster-Blockgröße für GS v 0
CHUNK_SIZE = 128              # Bytes pro BLE-Schreibvorgang
CHUNK_DELAY = 0.05            # Pause zwischen Paketen (≈2,5 kB/s, etwas schneller als der Druck)
PRINT_SPEED_MM_S = 6          # langsamste Herstellerangabe

DEFAULT_DENSITY = 4          # 1f 11 02 xx – höher = dunkler
DEFAULT_STROKE = 1           # Schrift 1 px kräftiger

CONF_ADDRESS = "address"

SERVICE_PRINT_TEXT = "print_text"
SERVICE_PRINT_IMAGE = "print_image"
SERVICE_PRINT_CALENDAR = "print_calendar"
SERVICE_FEED = "feed"
