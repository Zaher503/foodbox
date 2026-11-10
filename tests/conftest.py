import os
import tempfile

# Every test run gets its own empty data folder, whichever test file imports the app first.
# Without this, a file that imports fetch/main before setting FOODBOX_DATA uses the repo's real ./data.
os.environ["FOODBOX_DATA"] = tempfile.mkdtemp(prefix="foodbox-test-")
