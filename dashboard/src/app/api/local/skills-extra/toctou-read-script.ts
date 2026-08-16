// The TOCTOU-safe read script executed by `childRead` as `node -e <script>`.
//
// It lives in its own module (not `route.ts`) because a Next.js App Router
// route file may only export the HTTP method handlers plus the framework
// config exports (`runtime`, `dynamic`, …); any other named export fails
// `next build` with "does not match the required types of a Next.js Route".
// The read script must still be importable by a test that spawns this EXACT
// source against real fixtures (so the test can never drift from what runs in
// production), so it is exported from here and imported by both `route.ts` and
// the test.
//
// Behaviour: open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK) → fstat → require a regular
// file → fresh lstat → refuse on a symlink or a {dev,ino} identity mismatch
// (the achievable, adjacent-syscall narrowing of the accepted same-uid
// F-TOCTOU-PARENT residual) → only then read the pinned fd. O_NONBLOCK also
// makes a FIFO refuse immediately instead of blocking to the SIGKILL timeout.
export const TOCTOU_SAFE_READ_SCRIPT = `
(function () {
  const fs = require("node:fs");
  const path = process.argv[1];
  let fd;
  try {
    fd = fs.openSync(path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  } catch (err) {
    process.stderr.write(String((err && err.code) || err));
    process.exit(1);
  }
  let openedStat;
  try {
    openedStat = fs.fstatSync(fd);
  } catch (err) {
    fs.closeSync(fd);
    process.stderr.write(String((err && err.code) || err));
    process.exit(1);
  }
  if (!openedStat.isFile()) {
    fs.closeSync(fd);
    process.stderr.write("not a regular file");
    process.exit(1);
  }
  let freshLstat;
  try {
    freshLstat = fs.lstatSync(path);
  } catch (err) {
    fs.closeSync(fd);
    process.stderr.write(String((err && err.code) || err));
    process.exit(1);
  }
  if (freshLstat.isSymbolicLink() || freshLstat.dev !== openedStat.dev || freshLstat.ino !== openedStat.ino) {
    fs.closeSync(fd);
    process.stderr.write("identity mismatch after open");
    process.exit(1);
  }
  process.stdout.write(fs.readFileSync(fd));
  fs.closeSync(fd);
})();
`;
