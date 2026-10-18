import hub75
import picographics

# DISPLAY = picographics.DISPLAY_INTERSTATE75_64X32
# DISPLAY = picographics.DISPLAY_INTERSTATE75_96X32
# DISPLAY = picographics.DISPLAY_INTERSTATE75_128X32
DISPLAY = picographics.DISPLAY_INTERSTATE75_128X64
# DISPLAY = picographics.DISPLAY_INTERSTATE75_192X64
# DISPLAY = picographics.DISPLAY_INTERSTATE75_256X64

COLOR_ORDER = hub75.COLOR_ORDER_RGB
# COLOR_ORDER = hub75.COLOR_ORDER_RBG

# P2.5 128x64 1/32 scan panel with SM16380SH drivers
PANEL = hub75.PANEL_SM16380SH
# Optional, the default is SM16380SH_REG2. REG8 is brighter, but can show
# noise in large bright areas.
# SPWM_PROFILE = hub75.SM16380SH_REG8

# P2.5 128x64 1/64 scan panel with FM6373D drivers and a 6258K row driver:
# only R0, G0, B0 are used, and the row driver is a shift register
# PANEL = hub75.PANEL_FM6373
# SPWM_LANES = hub75.LANES_SINGLE
# SPWM_ROW_DRIVER = hub75.ROWS_SHIFT_REGISTER
