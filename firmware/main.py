# Frozen into the firmware, runs instead of a main.py on the filesystem.
# Hold button A while powering on or resetting to get to the REPL instead of
# starting the app, eg. if it crashes right away.
from machine import Pin

if Pin(14, Pin.IN, Pin.PULL_UP).value():
    import bvgdisp
else:
    print("button A held: not starting bvgdisp")
