#!/usr/bin/env python3
import asyncio
import argparse
import os
import sys
import shlex
import textwrap
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from bs4 import BeautifulSoup

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
except Exception as e:
    print("Playwright not available yet. Please install requirements first.")
    raise

console = Console(force_terminal=True, soft_wrap=True)


def normalize_url(url: str) -> str:
    if not url:
        return url
    if url.startswith(("http://", "https://")):
        return url
    # Treat bare domains as https by default
    return "https://" + url


class ConsoleBrowser:
    def __init__(self, user_data_dir: Optional[str] = None, headless: bool = True, render_mode: str = "html", max_chars: int = 200000):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.render_mode = render_mode  # 'html' | 'text'
        self.max_chars = max_chars
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self._last_highlight_selector = None
        self._current_frame = None  # type: ignore

    def _target(self):
        return self._current_frame if self._current_frame is not None else self.page

    def _locator(self, sel: str):
        target = self._target()
        return target.locator(sel)

    async def start(self):
        self._playwright = await async_playwright().start()
        # Use persistent context to preserve cookies/localstorage within sessions
        user_data_dir = self.user_data_dir or os.path.join(os.getcwd(), ".console_browser_userdata")
        os.makedirs(user_data_dir, exist_ok=True)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=self.headless,
            viewport={"width": 1280, "height": 800},
            accept_downloads=False,
        )
        # Use single page
        if len(self._context.pages) > 0:
            self.page = self._context.pages[0]
        else:
            self.page = await self._context.new_page()

        # Pipe page console messages to our console
        def _on_console_message(msg):
            try:
                msg_type = getattr(msg, "type", None)
                if callable(msg_type):
                    msg_type = msg_type()
                msg_text = getattr(msg, "text", None)
                if callable(msg_text):
                    msg_text = msg_text()
                console.print(Text(f"[page:console] {msg_type} - {msg_text}", style="dim"))
            except Exception:
                pass
        self.page.on("console", _on_console_message)

    async def close(self):
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()

    async def wait_settled(self, timeout_ms: int = 6000):
        try:
            await self.page.wait_for_load_state("load", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass
        # Small extra delay for SPA DOM updates
        await asyncio.sleep(0.2)

    async def goto(self, url: str):
        url = normalize_url(url)
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self.wait_settled()
            status = response.status if response else None
            console.print(Panel.fit(f"Navigated to {self.page.url} (status: {status})", title="goto", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to navigate to {url}: {e}", title="error", border_style="red"))

    async def reload(self):
        try:
            await self.page.reload(wait_until="domcontentloaded")
            await self.wait_settled()
        except Exception as e:
            console.print(Panel.fit(f"Failed to reload: {e}", title="error", border_style="red"))

    async def back(self):
        try:
            res = await self.page.go_back(wait_until="domcontentloaded")
            if res is None:
                console.print(Panel.fit("No previous page in history", title="back", border_style="yellow"))
            await self.wait_settled()
        except Exception as e:
            console.print(Panel.fit(f"Failed to go back: {e}", title="error", border_style="red"))

    async def forward(self):
        try:
            res = await self.page.go_forward(wait_until="domcontentloaded")
            if res is None:
                console.print(Panel.fit("No forward page in history", title="forward", border_style="yellow"))
            await self.wait_settled()
        except Exception as e:
            console.print(Panel.fit(f"Failed to go forward: {e}", title="error", border_style="red"))

    async def click(self, selector: str, nth: Optional[int] = None):
        # Allow plain CSS or XPath-like via prefix
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        # Highlight before click (for same-page)
        self._last_highlight_selector = sel
        try:
            await self._highlight(sel, nth)
        except Exception:
            pass
        # Try click with optional navigation expectation
        try:
            navigation_happened = False
            try:
                async with self.page.expect_navigation(wait_until="load", timeout=3000):
                    if nth is not None:
                        await self._locator(sel).nth(nth).click()
                    else:
                        await self._locator(sel).first.click()
                navigation_happened = True
            except PlaywrightTimeoutError:
                # No navigation; still perform click
                if nth is not None:
                    await self._locator(sel).nth(nth).click()
                else:
                    await self._locator(sel).first.click()
            await self.wait_settled()
            action = f"click {selector}" if nth is None else f"click {selector} {nth}"
            note = "(navigated)" if navigation_happened else ""
            console.print(Panel.fit(f"{action} {note}", title="click", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to click {selector}: {e}", title="error", border_style="red"))

    async def type_into(self, selector: str, text: str, clear: bool = True):
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        try:
            locator = self._locator(sel)
            await locator.first.wait_for(state="visible", timeout=5000)
            await locator.first.focus()
            if clear:
                try:
                    await locator.first.fill("")
                except Exception:
                    pass
            await locator.first.type(text, delay=20)
            await self.wait_settled()
            console.print(Panel.fit(f"typed into {selector}: {text}", title="type", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to type into {selector}: {e}", title="error", border_style="red"))

    async def eval_js(self, expression: str):
        try:
            target = self._target()
            result = await target.evaluate(f"() => (async () => {{ try {{ return await ( {expression} ); }} catch(e) {{ return 'Error: ' + e.message; }} }})()")
            console.print(Panel.fit(f"{result}", title="eval", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to eval JS: {e}", title="error", border_style="red"))

    async def press(self, key: str, selector: Optional[str] = None):
        try:
            if selector:
                sel = selector.strip()
                if sel.startswith("//") or sel.startswith("./"):
                    sel = f"xpath={sel}"
                locator = self._locator(sel).first
                await locator.wait_for(state="visible", timeout=5000)
                await locator.press(key)
            else:
                await self.page.keyboard.press(key)
            await self.wait_settled()
            target = selector or "page"
            console.print(Panel.fit(f"pressed {key} on {target}", title="press", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to press {key}: {e}", title="error", border_style="red"))

    async def waitfor(self, selector: str, state: str = "visible", timeout_ms: int = 10000):
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        try:
            await self._locator(sel).first.wait_for(state=state, timeout=timeout_ms)
            console.print(Panel.fit(f"waited for {selector} state={state}", title="waitfor", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to wait for {selector}: {e}", title="error", border_style="red"))

    async def fill(self, selector: str, text: str):
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        try:
            locator = self._locator(sel).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.fill(text)
            await self.wait_settled()
            console.print(Panel.fit(f"filled {selector}: {text}", title="fill", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to fill {selector}: {e}", title="error", border_style="red"))

    async def select(self, selector: str, value: str):
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        try:
            locator = self._locator(sel).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.select_option(value=value)
            await self.wait_settled()
            console.print(Panel.fit(f"selected {value} on {selector}", title="select", border_style="green"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to select on {selector}: {e}", title="error", border_style="red"))

    async def list_elements(self, selector: str, limit: int = 20):
        sel = selector.strip()
        if sel.startswith("//") or sel.startswith("./"):
            sel = f"xpath={sel}"
        script = r"""
            (arg) => {
                const sel = arg.sel;
                const lim = arg.lim;
                let elements = [];
                if (sel.startsWith('xpath=')) {
                    const xpath = sel.slice(6);
                    const itr = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_ITERATOR_TYPE, null);
                    let el; while ((el = itr.iterateNext())) { elements.push(el); }
                } else {
                    elements = Array.from(document.querySelectorAll(sel));
                }
                const items = elements.slice(0, lim).map((el, idx) => {
                    const name = el.tagName.toLowerCase();
                    const id = el.id || '';
                    const cls = (el.className && typeof el.className === 'string') ? el.className : '';
                    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 160);
                    const href = el.getAttribute && el.getAttribute('href');
                    const nameAttr = el.getAttribute && el.getAttribute('name');
                    const typeAttr = el.getAttribute && el.getAttribute('type');
                    return { idx, name, id, cls, text, href, nameAttr, typeAttr };
                });
                return { total: elements.length, items };
            }
        """
        try:
            target = self._target()
            data = await target.evaluate(script, {"sel": sel, "lim": limit})
            header = f"Found {data['total']} elements for selector: {selector} (showing up to {limit})"
            console.print(Panel.fit(header, title="list", border_style="blue"))
            for item in data["items"]:
                line = f"[{item['idx']}] <{item['name']}>"
                if item["id"]:
                    line += f" #{item['id']}"
                if item["cls"]:
                    line += f" .{item['cls'].replace(' ', '.')}"
                if item["typeAttr"]:
                    line += f" type={item['typeAttr']}"
                if item["nameAttr"]:
                    line += f" name={item['nameAttr']}"
                if item["href"]:
                    line += f" href={item['href']}"
                if item["text"]:
                    line += f" text=\"{item['text']}\""
                console.print(line)
        except Exception as e:
            console.print(Panel.fit(f"Failed to list {selector}: {e}", title="error", border_style="red"))

    async def list_frames(self):
        frames = self.page.frames
        lines = []
        for idx, fr in enumerate(frames):
            name = fr.name or ""
            url = fr.url
            marker = "*" if (self._current_frame is not None and fr == self._current_frame) else " "
            lines.append(f"{marker}[{idx}] name='{name}' url='{url}'")
        if not lines:
            console.print(Panel.fit("No frames", title="frames", border_style="blue"))
        else:
            console.print(Panel.fit("\n".join(lines), title="frames", border_style="blue"))

    async def use_frame(self, token: str):
        # token can be integer index, name=<name>, url=<substring>
        frames = self.page.frames
        chosen = None
        token = token.strip()
        if token.isdigit():
            idx = int(token)
            if 0 <= idx < len(frames):
                chosen = frames[idx]
        elif token.startswith("name="):
            name = token[len("name="):]
            for fr in frames:
                if (fr.name or "") == name:
                    chosen = fr
                    break
        elif token.startswith("url="):
            sub = token[len("url="):]
            for fr in frames:
                if sub in fr.url:
                    chosen = fr
                    break
        else:
            # try by exact name
            for fr in frames:
                if (fr.name or "") == token:
                    chosen = fr
                    break
            # or first url containing token
            if chosen is None:
                for fr in frames:
                    if token in fr.url:
                        chosen = fr
                        break
        if chosen is None:
            console.print(Panel.fit(f"Frame not found for token: {token}", title="useframe", border_style="red"))
            return
        self._current_frame = chosen
        console.print(Panel.fit(f"Using frame: name='{chosen.name}' url='{chosen.url}'", title="useframe", border_style="green"))

    async def use_main_frame(self):
        self._current_frame = None
        console.print(Panel.fit("Using main frame", title="useframe", border_style="green"))

    async def _highlight(self, sel: str, nth: Optional[int]):
        # Mark the element(s) with data-clicked for later rendering visibility
        script = r"""
            (arg) => {
                const sel = arg.sel;
                const nth = arg.nth;
                let elements = [];
                if (sel.startsWith('xpath=')) {
                    const xpath = sel.slice(6);
                    const itr = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_ITERATOR_TYPE, null);
                    let el; while ((el = itr.iterateNext())) { elements.push(el); }
                } else {
                    elements = Array.from(document.querySelectorAll(sel));
                }
                if (elements.length === 0) return 0;
                const setMark = (el) => {
                    try { el.setAttribute('data-console-clicked', 'true'); } catch(e) {}
                    try { el.style && (el.style.outline = '2px dashed red'); } catch(e) {}
                };
                if (typeof nth === 'number') {
                    const el = elements[nth]; if (el) setMark(el);
                    return el ? 1 : 0;
                } else {
                    elements.forEach(setMark);
                    return elements.length;
                }
            }
        """
        try:
            target = self._target()
            await target.evaluate(script, {"sel": sel, "nth": nth})
        except Exception:
            pass

    def _simplify_html_to_text(self, html: str) -> str:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for el in soup(["script", "style", "noscript"]):
                el.decompose()
            text = soup.get_text(separator="\n")
            lines = [ln.strip() for ln in text.splitlines()]
            lines = [ln for ln in lines if ln]
            return "\n".join(lines)
        except Exception:
            return html

    async def render(self):
        html = await self.page.content()
        if self.render_mode == "text":
            out = self._simplify_html_to_text(html)
        else:
            out = html
        if len(out) > self.max_chars:
            clipped = out[: self.max_chars]
            clipped += f"\n\n[... clipped {len(out) - self.max_chars} chars ...]"
            out = clipped
        # Add URL header
        header = Text(f"URL: {self.page.url}", style="bold cyan")
        console.print(Panel(header, border_style="cyan"))
        console.print(out)

    def usage(self) -> str:
        return textwrap.dedent(
            """
            Commands:
              - goto <url>                : navigate to a URL
              - back                      : go back in history
              - forward                   : go forward in history
              - reload                    : reload current page
              - click <selector> [nth]    : click element by CSS or XPath (prefix with // for XPath). Optional nth index (0-based)
              - type <selector> <text>    : type text into an input/textarea element
              - fill <selector> <text>    : fill value directly into an input/textarea
              - select <selector> <value> : select an option value in a <select>
              - press <key>               : press a key on the page (e.g., Enter)
              - press <selector> <key>    : press a key on an element (e.g., input then Enter)
                             - waitfor <selector> [state|timeout_ms] [timeout_ms]: wait for selector state (attached|detached|visible|hidden) or specify a timeout directly
              - list <selector> [limit]   : list matching elements with indices and brief info
              - eval <js>                 : evaluate JavaScript in the page context
              - view [html|text]          : switch render mode (html default or simplified text)
              - wait <ms>                 : wait for milliseconds
              - title                     : print document.title
              - url                       : print current URL
              - frames                    : list frames and current selection
              - useframe <idx|name=|url=>: switch to a frame by index, exact name, or url substring
              - usemainframe              : switch to main page context
              - help                      : show this help
              - exit                      : quit

            Selector notes:
              - CSS examples: a#login, button.submit, input[name="q"]
              - XPath examples: //a[contains(., 'Next')], (//button)[1]
            """
        ).strip()


async def repl(browser: ConsoleBrowser, preloaded_command: Optional[str] = None):
    async def run_and_render(action_coro):
        await action_coro
        await browser.render()

    # Initial render if a page is already open (about:blank otherwise)
    await browser.render()

    if preloaded_command:
        await handle_command(browser, preloaded_command)
        return

    while True:
        try:
            prompt = Text(f"browser[{browser.page.url}]> ", style="bold green")
            console.print(prompt, end="")
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            await handle_command(browser, line)
        except KeyboardInterrupt:
            console.print("Exiting...")
            break


async def handle_command(browser: ConsoleBrowser, line: str):
    parts = shlex.split(line)
    if not parts:
        return
    cmd = parts[0].lower()

    if cmd in ("exit", "quit", ":q"):
        await browser.close()
        sys.exit(0)

    if cmd in ("help", "h", "?"):
        console.print(Panel.fit(browser.usage(), title="help", border_style="blue"))
        return

    if cmd == "view":
        mode = parts[1] if len(parts) > 1 else None
        if mode in ("html", "text"):
            browser.render_mode = mode
            console.print(Panel.fit(f"Render mode set to {mode}", title="view", border_style="green"))
        else:
            console.print(Panel.fit("Usage: view [html|text]", title="view", border_style="yellow"))
        await browser.render()
        return

    if cmd == "goto" and len(parts) >= 2:
        await browser.goto(" ".join(parts[1:]))
        await browser.render()
        return

    if cmd == "back":
        await browser.back()
        await browser.render()
        return

    if cmd == "forward":
        await browser.forward()
        await browser.render()
        return

    if cmd == "reload":
        await browser.reload()
        await browser.render()
        return

    if cmd == "click" and len(parts) >= 2:
        nth = None
        if len(parts) >= 3 and parts[-1].isdigit():
            # if last token is integer, treat as index
            nth = int(parts[-1])
            selector = " ".join(parts[1:-1])
        else:
            selector = " ".join(parts[1:])
        await browser.click(selector, nth=nth)
        await browser.render()
        return

    if cmd == "type" and len(parts) >= 3:
        selector = parts[1]
        text = " ".join(parts[2:])
        await browser.type_into(selector, text)
        await browser.render()
        return

    if cmd == "fill" and len(parts) >= 3:
        selector = parts[1]
        text = " ".join(parts[2:])
        await browser.fill(selector, text)
        await browser.render()
        return

    if cmd == "select" and len(parts) >= 3:
        selector = parts[1]
        value = " ".join(parts[2:])
        await browser.select(selector, value)
        await browser.render()
        return

    if cmd == "press" and len(parts) >= 2:
        if len(parts) == 2:
            # page-level key
            key = parts[1]
            await browser.press(key)
        else:
            selector = parts[1]
            key = " ".join(parts[2:])
            await browser.press(key, selector=selector)
        await browser.render()
        return

    if cmd == "waitfor" and len(parts) >= 2:
        # Support selectors containing spaces by parsing from the end
        allowed_states = {"attached", "detached", "visible", "hidden"}
        state = "visible"
        timeout_ms = 10000
        tail = []
        if parts[-1].isdigit():
            timeout_ms = int(parts[-1])
            tail.append(parts[-1])
        if len(parts) - len(tail) > 2 and parts[-1 - len(tail)] in allowed_states:
            state = parts[-1 - len(tail)]
            tail.append(parts[-1 - len(tail)])
        # selector is everything between cmd and tail
        sel_end = len(parts) - len(tail)
        selector = " ".join(parts[1:sel_end])
        await browser.waitfor(selector, state=state, timeout_ms=timeout_ms)
        await browser.render()
        return

    if cmd == "list" and len(parts) >= 2:
        selector = parts[1]
        limit = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 20
        await browser.list_elements(selector, limit=limit)
        await browser.render()
        return

    if cmd == "eval" and len(parts) >= 2:
        js = line[len("eval "):]
        await browser.eval_js(js)
        await browser.render()
        return

    if cmd == "wait" and len(parts) >= 2:
        try:
            ms = int(parts[1])
            await asyncio.sleep(ms / 1000.0)
        except ValueError:
            console.print(Panel.fit("Usage: wait <ms>", title="wait", border_style="yellow"))
        await browser.render()
        return

    if cmd == "title":
        try:
            title = await browser.page.title()
            console.print(Panel.fit(title, title="title", border_style="blue"))
        except Exception as e:
            console.print(Panel.fit(f"Failed to get title: {e}", title="error", border_style="red"))
        await browser.render()
        return

    if cmd == "url":
        console.print(Panel.fit(browser.page.url, title="url", border_style="blue"))
        await browser.render()
        return

    if cmd == "frames":
        await browser.list_frames()
        await browser.render()
        return

    if cmd == "useframe" and len(parts) >= 2:
        token = " ".join(parts[1:])
        await browser.use_frame(token)
        await browser.render()
        return

    if cmd == "usemainframe":
        await browser.use_main_frame()
        await browser.render()
        return

    console.print(Panel.fit(f"Unknown or malformed command: {line}\n\n{browser.usage()}", title="error", border_style="red"))


async def ensure_playwright_browsers():
    # Ensure Chromium is installed by attempting a lightweight launch; if it fails, install browsers.
    try:
        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=True)
                await browser.close()
                return
            except Exception:
                pass
    except Exception:
        pass

    console.print(Panel.fit("Installing Playwright browsers (chromium)...", title="setup", border_style="blue"))
    import subprocess
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    except Exception as e:
        console.print(Panel.fit(f"Failed to auto-install browsers: {e}", title="setup", border_style="red"))


def parse_args():
    parser = argparse.ArgumentParser(description="Console-based web browser (headless, Playwright)")
    parser.add_argument("--headless", action="store_true", default=True, help="Run headless (default)")
    parser.add_argument("--headed", action="store_true", help="Run with UI (if available)")
    parser.add_argument("--render", choices=["html", "text"], default="html", help="Render mode")
    parser.add_argument("--max-chars", type=int, default=200000, help="Max characters to print per render")
    parser.add_argument("--user-data-dir", type=str, default=None, help="Persistent user data directory")
    parser.add_argument("--once", type=str, default=None, help="Run a single command and exit (e.g., \"goto https://example.com\")")
    parser.add_argument("--url", type=str, default=None, help="Initial URL to open")
    return parser.parse_args()


async def main():
    args = parse_args()
    await ensure_playwright_browsers()

    headless = True
    if args.headed:
        headless = False

    browser = ConsoleBrowser(
        user_data_dir=args.user_data_dir,
        headless=headless,
        render_mode=args.render,
        max_chars=args.max_chars,
    )

    await browser.start()

    # If an initial URL is provided, navigate first
    if args.url:
        await browser.goto(args.url)

    try:
        await repl(browser, preloaded_command=args.once)
    finally:
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass