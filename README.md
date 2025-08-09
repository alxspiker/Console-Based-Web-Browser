# Console Browser (Headless, Playwright)

A console-based web browser written in Python using Playwright. It supports:

- Navigation (goto, back, forward, reload)
- JavaScript execution and SPA/AJAX updates
- Clicking elements by CSS/XPath selectors
- Typing into form fields
- Rendering page HTML (or simplified text) in the terminal
- Session persistence via a user data directory

## Setup

```bash
pip install -r requirements.txt --break-system-packages
python3 -m playwright install chromium
```

## Run

```bash
python console_browser.py --url https://example.com
```

Or run a single command and exit:

```bash
python console_browser.py --once "goto https://example.com"
```

## Commands

- `goto <url>`: navigate to a URL (http/https implied if missing)
- `back`: go back in history
- `forward`: go forward in history
- `reload`: reload current page
- `click <selector> [nth]`: click an element by CSS or XPath (prefix XPath with `//`). Optional `nth` is 0-based index
- `type <selector> <text>`: type text into the first matching input/textarea
- `fill <selector> <text>`: set value directly on input/textarea
- `select <selector> <value>`: select an option in a `<select>`
- `press <key>` or `press <selector> <key>`: press a key globally or on an element (e.g., `Enter`)
- `waitfor <selector> [state|timeout_ms] [timeout_ms]`: wait for selector state (`attached|detached|visible|hidden`) or specify a timeout directly
- `list <selector> [limit]`: list matching elements with indices and brief info
- `eval <js>`: evaluate JavaScript and print the result
- `view [html|text]`: switch rendering mode
- `wait <ms>`: pause for the specified milliseconds
- `title`: print document.title
- `url`: print current URL
- `frames`: list frames and current frame selection
- `useframe <idx|name=|url=>`: switch to a frame by index, exact name, or URL substring
- `usemainframe`: switch context back to the main frame
- `help`: show help
- `exit`: quit

## Notes

- For XPath, use selectors like `//a[contains(., 'Next')]` or `(//button)[1]`.
- The last clicked element is marked with `data-console-clicked="true"` and an outline in the DOM (if still on the same page).
- Rendering is clipped to `--max-chars` characters to avoid flooding the terminal.
- Use `--user-data-dir` to persist cookies/local storage across runs.
- Some sites may present bot/CAPTCHA challenges which can block automated navigation. Use `view text` to simplify output when needed.