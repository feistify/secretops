# Recording of the demo in docs/DEMO.md; change the two together.
#
# Run from the repository root, on a throwaway machine with an installed
# broker, a test Infisical project and a test GitHub repository:
#
#   DEMO_OWNER=example-owner DEMO_REPO=example-repo vhs docs/demo/demo.tape
#
# Before recording:
# - The recording account is both the human and the agent account
#   (install.sh --agent-user for that account). Prompts show who acts.
# - sudo must not ask for a password during the recording; never put a
#   password in this file.
# - DEMO_API_KEY must not exist yet in the vault's dev environment.
# - Needs a vhs version with the Wait command.
#
# The value typed below is a made-up string, hidden by the prompt. Delete
# DEMO_API_KEY from Infisical and GitHub afterwards.

Output docs/images/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1280
Set Height 720
Set Padding 24
Set Framerate 20
Set TypingSpeed 35ms
Set WaitPattern /\$\s*$/
Set WaitTimeout 60s

Hide
Type "source docs/demo/demo-env.sh && clear" Enter
Wait
Show

# 1. The human stores the value; nothing is echoed.
Type "secretops add DEMO_API_KEY --env dev" Enter
Sleep 4s
Type "made-up-demo-value-not-a-real-key" Enter
Sleep 3s
Type "y" Enter
Wait
Sleep 3s

# 2. The agent uploads a recipe and saves a plan: names only.
Hide
Type "as_agent && clear" Enter
Wait
Show
Type "cat demo-sync.yaml" Enter
Wait
Sleep 4s
Type "secretops-local write-recipe demo-sync.yaml < demo-sync.yaml" Enter
Wait
Sleep 1s
Type "secretops-local plan --file demo-sync.yaml --save" Enter
Wait
Sleep 3s
Type "secretops-local preflight-plan --id $PLAN" Enter
Wait
Sleep 3s

# 3. The human reviews and approves; the agent cannot.
Hide
Type "as_you && clear" Enter
Wait
Show
Type "sudo -u secretops-broker /usr/local/bin/secretops-broker show-plan --id $PLAN" Enter
Wait
Sleep 6s
Type "sudo -u secretops-broker /usr/local/bin/secretops-broker approve-plan --id $PLAN" Enter
Wait
Sleep 3s

# 4. The agent runs the approved plan, then checks the name in GitHub.
Hide
Type "as_agent && clear" Enter
Wait
Show
Type "secretops-local execute-plan --id $PLAN" Enter
Wait
Sleep 3s
Type "secretops-local write-recipe demo-verify.yaml < demo-verify.yaml" Enter
Wait
Sleep 1s
Type "secretops-local plan --file demo-verify.yaml --save" Enter
Wait
Sleep 2s
Type "secretops-local execute-plan --id $PLAN" Enter
Wait
Sleep 6s
