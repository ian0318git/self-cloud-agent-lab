#!/usr/bin/env python3
"""第四刀：在暫存樹重現修補，產出 scripts/endpoint-apikey-selfheal.patch。

**為什麼是行號定址而不是字串比對。** 要刪的那個死分支裡有一個 `\\u2192` 逃脫序
（`console.print("  [dim]\\u2192 ...")`），用字串比對的話，寫錯一層跳脫就會安靜地
比對失敗。行號定址在這裡更硬：每一段都先斷言「這一行長什麼樣」，對不上就大聲停。

**由下往上改。** 刪掉 7 行之後，後面所有行號都會位移；從最大的行號開始改，前面
的座標才不會失效。
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("/home/ian/github-project/self-cloud-agent-lab")
STAGE = REPO / "tmp" / "selfheal-stage"
TOOL = Path.home() / ".local/share/uv/tools/endpoint-vps"
FILES = ["engine/engine.py", "endpoint/commands.py"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def locate_site_packages() -> Path:
    hit = subprocess.run(
        ["find", str(TOOL), "-path", "*/site-packages/endpoint/commands.py", "-print", "-quit"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not hit:
        sys.exit("找不到 site-packages")
    return Path(hit).parent.parent


def expect(lines: list[str], n: int, needle: str) -> None:
    """斷言第 n 行（1-indexed）含 needle。行號位移會在這裡被擋下來。"""
    got = lines[n - 1]
    if needle not in got:
        sys.exit(f"座標位移：第 {n} 行預期含 {needle!r}\n  實得：{got!r}")


# --------------------------------------------------------------------------
# 各段的替換內容（由下往上套用）
# --------------------------------------------------------------------------

# --- endpoint/commands.py ---

# 2634-2640：run_register 的自癒點
REGISTER_OLD = """    try:
        client = VPSClient(tunnel_url)
        api_key = client.get_api_key()
    except Exception as e:
        console.err(f"Could not fetch API key from VPS: {e}")
        return
    save_cached_apikey(api_key)
"""
REGISTER_NEW = """    # Same source as _register_with_proxy (D-070): the local key written at
    # boot. There is nothing to fetch over the tunnel any more.
    api_key = config.api_key or load_cached_apikey() or ""
    if not api_key:
        console.err("No local API key configured — nothing to register with.")
        console.dim("  Run 'endpoint boot' once to provision one.")
        return
    save_cached_apikey(api_key)
"""

# 2169-2171：401 提示
WARN_OLD = """            console.warn(
                "Authentication denied by proxy — run 'endpoint register' for a fresh key."
            )
"""
WARN_NEW = """            console.warn(
                "Authentication denied — the key this machine holds is not the "
                "one the engine was baked with."
            )
            console.dim(
                "  Re-run 'endpoint boot' so the engine is rebuilt with the local key."
            )
"""

# 2157-2162：run_provider_config 的取金鑰處
PROVIDER_OLD = """    # Use cached key from boot signal first (avoids proxy 401 from missing auth)
    api_key = load_cached_apikey() or ""
    if api_key:
        client._session.headers.update({"Authorization": f"Bearer {api_key}"})
    else:
        console.warn("No cached API key — boot may have failed to broadcast it.")
"""
PROVIDER_NEW = """    # Authenticate with the local key. VPSClient already installed the cached
    # key on construction; this re-installs the authoritative one so a stale
    # cache cannot shadow it (D-070).
    api_key = config.api_key or load_cached_apikey() or ""
    if api_key:
        client._session.headers.update({"Authorization": f"Bearer {api_key}"})
    else:
        console.warn("No local API key configured — run 'endpoint boot' to provision one.")
"""

# 1481-1500：_register_with_proxy 的自癒點一
WITH_PROXY_OLD = """    # Try cached API key first (broadcast by engine via ntfy.sh during startup).
    # This avoids DNS-resolution failures on the dynamically-assigned
    # trycloudflare.com tunnel hostname from the user's machine.
    api_key = load_cached_apikey()
    if not api_key:
        # Fallback: fetch from engine via tunnel URL (4 retries, exp backoff)
        for attempt in range(4):
            try:
                client = VPSClient(tunnel_url, timeout=5)
                api_key = client.get_api_key()
                break
            except Exception as e:
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                console.warn(f"Proxy registration: could not reach engine ({e})")
                console.dim("Run 'endpoint register' once the VPS is online.")
                return config.proxy_url if config.proxy_url else tunnel_url
    if not api_key:
        return config.proxy_url if config.proxy_url else tunnel_url
"""
WITH_PROXY_NEW = """    # The authoritative key is the local one: `identity.api_key` in
    # endpoint-config.yaml, provisioned by run_boot() and baked into the engine.
    # D-070 removed the tunnel round-trip that used to fetch a key from the
    # engine when the cache was empty -- /v1/apikey is no longer in the auth
    # bypass, so that request can only ever return a key we already hold, and it
    # fails outright when we hold none. Reading it locally also sidesteps the
    # DNS-resolution failures on the dynamically-assigned trycloudflare.com
    # tunnel hostname, which is why the cache was consulted first.
    api_key = config.api_key or load_cached_apikey() or ""
    if not api_key:
        console.warn("Proxy registration: no local API key configured.")
        console.dim("  Run 'endpoint boot' once to provision one.")
        return config.proxy_url if config.proxy_url else tunnel_url
"""

# 1281-1287：切 A 留下的死分支（產生端已被 D-067 移除）
DEAD_BRANCH_LINES = 7

# --- engine/engine.py ---

# 1624：免認證清單
BYPASS_OLD = """    bypass = {"/health", "/metrics", "/tunnel", "/docs", "/openapi.json", "/v1/apikey"}
"""
BYPASS_NEW = """    # `/v1/apikey` is deliberately absent (D-070): it hands back the key, so
    # leaving it unauthenticated made the tunnel URL alone sufficient. The route
    # itself is kept -- an authenticated caller can confirm which key the engine
    # expects -- but it is no longer a way in.
    bypass = {"/health", "/metrics", "/tunnel", "/docs", "/openapi.json"}
"""

# 1929-1931：處理器（保留，補上「現在要認證」的說明）
HANDLER_OLD = """@app.get("/v1/apikey")
async def apikey() -> Any:
    return {"key": _get_api_key()}
"""
HANDLER_NEW = """@app.get("/v1/apikey")
async def apikey() -> Any:
    # Auth-gated since D-070 (no longer in the bypass set above). This is now an
    # introspection route: a caller who already holds the key can check which
    # key the engine expects. It can no longer *obtain* one.
    return {"key": _get_api_key()}
"""


def edit_commands(text: str) -> str:
    lines = text.splitlines(keepends=True)

    # 由下往上
    expect(lines, 2634, "try:")
    expect(lines, 2640, "save_cached_apikey(api_key)")
    assert "".join(lines[2633:2640]) == REGISTER_OLD, "2640-區塊不符"
    lines[2633:2640] = [REGISTER_NEW]

    expect(lines, 2169, "console.warn(")
    expect(lines, 2170, "endpoint register")
    expect(lines, 2171, ")")
    assert "".join(lines[2168:2171]) == WARN_OLD, "401-區塊不符"
    lines[2168:2171] = [WARN_NEW]

    expect(lines, 2157, "boot signal first")
    expect(lines, 2162, "failed to broadcast it")
    assert "".join(lines[2156:2162]) == PROVIDER_OLD, "provider-區塊不符"
    lines[2156:2162] = [PROVIDER_NEW]

    expect(lines, 1481, "broadcast by engine via ntfy.sh")
    expect(lines, 1500, "return config.proxy_url if config.proxy_url else tunnel_url")
    assert "".join(lines[1480:1500]) == WITH_PROXY_OLD, "_register_with_proxy-區塊不符"
    lines[1480:1500] = [WITH_PROXY_NEW]

    expect(lines, 1281, 'signal.startswith("APIKEY:")')
    expect(lines, 1285, "save_cached_apikey(key)")
    expect(lines, 1287, "continue")
    # 確認刪掉的正好是那 7 行，不是「7 行」而已：下一行必須是 TUNNEL 分支。
    # （這裡原本猜是空行，被自己的斷言擋下來 —— 它其實緊接著。斷言寫對了。）
    expect(lines, 1288, 'if signal.startswith("TUNNEL:")')
    del lines[1280:1280 + DEAD_BRANCH_LINES]

    return "".join(lines)


def edit_engine(text: str) -> str:
    lines = text.splitlines(keepends=True)

    expect(lines, 1929, '@app.get("/v1/apikey")')
    assert "".join(lines[1928:1931]) == HANDLER_OLD, "handler-區塊不符"
    lines[1928:1931] = [HANDLER_NEW]

    expect(lines, 1624, "bypass = {")
    assert "".join(lines[1623:1624]) == BYPASS_OLD, "bypass-區塊不符"
    lines[1623:1624] = [BYPASS_NEW]

    return "".join(lines)


def main() -> None:
    site = locate_site_packages()
    print(f"來源：{site}")

    if STAGE.exists():
        shutil.rmtree(STAGE)

    pristine, staged = {}, {}
    for rel in FILES:
        src = site / rel
        if not src.is_file():
            sys.exit(f"找不到 {src}")
        pristine[rel] = src.read_text()
        (STAGE / rel).parent.mkdir(parents=True, exist_ok=True)
        (STAGE / rel).write_text(pristine[rel])

    (STAGE / "engine/engine.py").write_text(edit_engine(pristine["engine/engine.py"]))
    (STAGE / "endpoint/commands.py").write_text(edit_commands(pristine["endpoint/commands.py"]))

    print()
    for rel in FILES:
        a, b = pristine[rel].splitlines(keepends=True), (STAGE / rel).read_text().splitlines(keepends=True)
        print(f"{rel}: {len(a)} -> {len(b)} 行（{len(b) - len(a):+d}）")

    chunks = []
    for rel in FILES:
        diff = difflib.unified_diff(
            pristine[rel].splitlines(keepends=True),
            (STAGE / rel).read_text().splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3,
        )
        chunks.append("".join(diff))

    patch = "".join(chunks)
    # difflib 的 @@ 區塊沒有行尾反斜線問題時就是合法的 unified diff；
    # 但缺 "\ No newline at end of file" 的處理，這裡兩個檔尾都有換行，所以不需要。
    out = REPO / "scripts/endpoint-apikey-selfheal.patch"
    out.write_text(patch)
    print(f"\n已寫入 {out}（{len(patch)} bytes）")

    print("\n--- 雜湊 ---")
    for rel in FILES:
        print(f"{rel}\n  pristine {sha(site / rel)}\n  patched  {sha(STAGE / rel)}")


if __name__ == "__main__":
    main()
