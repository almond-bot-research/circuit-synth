"""Device models, written from the manufacturers' datasheets.

Each model records what a driver does that the datasheet forbids or does
not define in `violations`; tests should require that list to stay empty.

    mcp2518fd   Microchip MCP2518FD CAN FD controller (SPI)
    w5500       WIZnet W5500 Ethernet controller (SPI), UDP sockets, RSTn pin
"""

MODELS = {
    "mcp2518fd": "Microchip MCP2518FD CAN FD controller (SPI): registers, FIFOs and RAM, modes, INT / GPIO pins",
    "w5500": "WIZnet W5500 Ethernet controller (SPI): common and socket registers, buffers, UDP sockets, link",
}
