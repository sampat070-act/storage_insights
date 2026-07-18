# Regenerates the README's demo GIF from a scripted terminal session, so
# it stays reproducible instead of a one-off manual screen recording.
#
# Usage: vhs demo.tape
# Requires: brew install vhs, and the project venv already set up
# (pip install -r requirements.txt).

Output demo.gif

Require echo

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 720
Set Theme "Dracula"
Set Padding 20
Set WindowBar Colorful

Type "venv/bin/python3 demo.py"
Sleep 500ms
Enter

Sleep 12s
