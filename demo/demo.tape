# The README's demo GIF (docs/assets/demo.gif), recorded with VHS (https://github.com/charmbracelet/vhs)
# from a real run. demo/record_gif.sh prepares the project, records this tape and cleans up.

Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1280
Set Height 760
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms

Hide
Type "cd $DEMO_OUT/acme && export PATH=$DEMO_VENV_BIN:$PATH PS1='$ ' && clear"
Enter
Sleep 1s
Show

Type "# One line of the agent's system prompt changes:"
Enter
Type "diff -U0 ../system.md.orig prompts/system.md | tail -n +3"
Enter
Sleep 4s

Type "hecklr run --suite smoke | sed -n '1,11p'"
Enter
Sleep 18s

Type "clear && hecklr gate --suite smoke"
Enter
Sleep 12s
