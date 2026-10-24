"""对着**真的跑起来的服务**把 U67~U71 逐条走一遍。

为什么还要这么一个脚本
----------------------
`tests/test_accounts_api.py` 用 ASGI 直连，证明的是「路由与装配对不对」。
它证明不了「进程起来、连上 Redis/Postgres、把东西写进磁盘之后还能不能用」——
而那才是用户真会遇到的那一层。所以这里走**真的 TCP**。

判据都是可机检的（注册 → 昵称 → 选模型 → 定位推荐 → 导航 → 改密 → 遗忘重置 → 访客越界），
每个检查都对应需求里的一句原话，失败会打印出实际值而不是一句「失败」。

用法
----
    # 起服务（另开一个终端）
    AIBUTLER_ENV=.env.dev .venv/Scripts/python.exe -m uvicorn \
        ai_butler.server:build_app --factory --port 8411
    # 跑本脚本
    .venv/Scripts/python.exe scripts/smoke_accounts.py http://127.0.0.1:8411

退出码：0 全过 / 1 有检查失败 / 2 服务连不上。

刻意不写死任何手机号与口令
--------------------------
标识与口令全部运行时随机生成。把「看起来像真号」的字面量写进仓库，
既会触发本机安全审批，也会让人误以为那是测试账号。
"""

from __future__ import annotations

import json
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8411"
RESULTS: list[tuple[bool, str, str]] = []


def call(method: str, path: str, body=None, token: str | None = None,
         timeout: int = 30) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200]}
    except urllib.error.URLError as exc:
        print(f"连不上 {BASE}：{exc}")
        print("先起服务：AIBUTLER_ENV=.env.dev .venv/Scripts/python.exe -m uvicorn "
              "ai_butler.server:build_app --factory --port 8411")
        raise SystemExit(2)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(("PASS  " if ok else "FAIL  ") + name + (("  | " + detail) if detail else ""))


def stream(path: str, body: dict, token: str, timeout: int = 300) -> list[dict]:
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    req.add_header("Authorization", "Bearer " + token)
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def section(title: str) -> None:
    print("\n== " + title + " ==")


def main() -> int:
    ident = "138" + "".join(secrets.choice("0123456789") for _ in range(8))
    pw1, pw2, pw3 = ("Pw-" + secrets.token_hex(6) for _ in range(3))

    section("① 注册与隐私：手机号做账号，落盘与响应都不得出现明文")
    st, reg = call("POST", "/api/session/register", {
        "kind": "phone", "identifier": ident, "password": pw1,
        "device_id": "smoke-device",
    })
    token = reg.get("token", "")
    profile = reg.get("profile") or {}
    check("注册成功并直接签发会话", st == 201 and bool(token), f"status={st}")
    check("注册响应不含明文手机号", ident not in json.dumps(reg, ensure_ascii=False))
    check("注册响应只给掩码标识",
          "*" in str(profile.get("ident_masked", "")), str(profile.get("ident_masked")))
    check("未指定昵称时自动随机", bool(profile.get("nickname")), str(profile.get("nickname")))

    st, dup = call("POST", "/api/session/register", {
        "kind": "phone", "identifier": ident, "password": pw1, "device_id": "d2"})
    check("同号重复注册被拒", st == 409 and dup.get("code") == "identifier_taken", f"status={st}")

    section("② 昵称：自定义 / 随机刷新 / 注册前提建议")
    st, me = call("GET", "/api/accounts/me", token=token)
    check("账号资料可读且只含掩码",
          st == 200 and me.get("ident_masked") == profile.get("ident_masked"))
    check("档案里没有密文/摘要/口令哈希",
          not ({"ident_cipher", "ident_digest", "password_hash"} & set(me)))

    st, nick = call("POST", "/api/accounts/nickname", {"nickname": "夜航船"}, token=token)
    check("昵称可自定义", st == 200 and nick.get("nickname") == "夜航船", str(nick.get("nickname")))
    st, bad = call("POST", "/api/accounts/nickname", {"nickname": ""}, token=token)
    check("非法昵称被拒", st == 400 and bad.get("code") == "invalid_nickname", str(bad.get("code")))
    st, rnd = call("POST", "/api/accounts/nickname/random", {}, token=token)
    check("昵称可随机刷新",
          st == 200 and bool(rnd.get("nickname")) and rnd["nickname"] != "夜航船",
          str(rnd.get("nickname")))
    st, sug = call("POST", "/api/accounts/nickname/suggest", {})
    check("注册前可取随机昵称建议（公开接口）", st == 200 and bool(sug.get("nickname")))

    section("③ 自选模型：目录 / 默认未绑定 / 自带 key / 掩码 / 解绑")
    st, models = call("GET", "/api/models", token=token)
    catalog_items = models.get("models") or []
    check("可选出模型目录",
          st == 200 and bool(catalog_items)
          and all(i.get("model_key") and i.get("label") for i in catalog_items),
          ",".join(f"{i.get('model_key')}({i.get('tier')})" for i in catalog_items))
    check("默认未绑定（走画像匹配）", models.get("binding") in (None, {}))
    check("额度可见", "quota" in models, json.dumps(models.get("quota"), ensure_ascii=False))

    secret_key = "sk-" + secrets.token_hex(16)
    st, byok = call("POST", "/api/models/byok", {
        "base_url": "https://api.example.com/v1", "model": "smoke-model",
        "api_key": secret_key, "display_name": "我的模型"}, token=token)
    bound = byok.get("binding") or {}
    check("可自带 API Key 接入新模型", st == 201, f"status={st}")
    check("BYOK 响应不含明文 key", secret_key not in json.dumps(byok, ensure_ascii=False))
    check("BYOK 只回掩码", "*" in str(bound.get("api_key_hint", "")), str(bound.get("api_key_hint")))
    check("BYOK 绑定视图不含密文", "api_key_cipher" not in bound)

    st, models2 = call("GET", "/api/models", token=token)
    bound2 = models2.get("binding") or {}
    check("绑定生效且列表里也只有掩码",
          str(bound2.get("model_key", "")).startswith("byok-")
          and secret_key not in json.dumps(models2, ensure_ascii=False),
          f"{bound2.get('model_key')} / {bound2.get('api_key_hint')}")
    st, removed = call("DELETE", "/api/models", token=token)
    check("可解除绑定", st == 200 and removed.get("removed") is True, str(removed))

    section("④ 定位 / 推荐 / 导航")
    lat, lon = 39.908700, 116.397500
    st, rec = call("POST", "/api/location/recommend", {
        "lat": lat, "lon": lon, "prefs": ["历史", "美食"], "limit": 3}, token=token)
    items = rec.get("items") or []
    check("按所在地+喜好出推荐", st == 200 and len(items) > 0, f"n={len(items)}")
    check("每条推荐都带理由", all(i.get("reason") for i in items))
    grid = rec.get("located_at_grid") or []
    check("坐标上报前已粗化",
          bool(grid) and len(str(grid[0]).split(".")[-1]) <= 3, str(grid))
    check("无地图 key 时如实声明", rec.get("map_available") is False, str(rec.get("map_note")))
    check("示例数据如实标注", rec.get("data_source") == "seed-demo", str(rec.get("data_source")))

    st, nav = call("POST", "/api/location/navigation", {
        "dest_lat": 39.916300, "dest_lon": 116.397200, "dest_name": "故宫博物院"}, token=token)
    links = nav.get("links") or []
    check("能生成三大导航 App 链接", st == 200 and len(links) >= 3,
          ",".join(str(x.get("app")) for x in links))
    check("链接按目标 App 用对坐标系", all(x.get("coordinate_system") for x in links),
          ",".join(f"{x.get('app')}:{x.get('coordinate_system')}" for x in links))
    check("起点默认不外传（尊重隐私）", nav.get("origin_shared") is False)

    section("⑤ 默认免费模型真的能答，额度按账号扣")
    st, before = call("GET", "/api/models", token=token)
    free_before = (before.get("quota") or {}).get("free_remaining")
    events = stream("/api/chat/stream", {"utterance": "用一句话介绍你自己"}, token)
    kinds = [e.get("type") for e in events]
    answer = "".join(e.get("text", "") for e in events if e.get("type") in ("chunk", "done"))
    errors = [e for e in events if e.get("type") == "error"]
    check("对话链路能出流", bool(events) and kinds[0] == "accepted", str(kinds[:4]))
    check("默认模型给出了回答", bool(answer.strip()) and not errors,
          (answer or json.dumps(errors[:1], ensure_ascii=False))[:80].replace("\n", " "))
    st, after = call("GET", "/api/models", token=token)
    free_after = (after.get("quota") or {}).get("free_remaining")
    check("默认路径也扣额度（不是白调）",
          free_before is not None and free_after == free_before - 1,
          f"{free_before} → {free_after}")

    section("⑥ 会话与口令：改密吊销、遗忘重置、不可枚举")
    st, _ = call("POST", "/api/accounts/password",
                 {"old_password": pw1, "new_password": pw2}, token=token)
    check("登录态可改密码", st == 200, f"status={st}")
    st, _ = call("GET", "/api/accounts/me", token=token)
    check("改密后旧会话立刻失效", st == 401, f"status={st}")

    st, login2 = call("POST", "/api/session/login",
                      {"kind": "phone", "identifier": ident, "password": pw2,
                       "device_id": "smoke-device"})
    check("新密码可登录", st == 200 and bool(login2.get("token")), f"status={st}")

    _, wrong = call("POST", "/api/session/login",
                    {"kind": "phone", "identifier": ident, "password": pw2 + "x",
                     "device_id": "smoke-device"})
    _, missing = call("POST", "/api/session/login",
                      {"kind": "phone", "identifier": "13900000000",
                       "password": pw2, "device_id": "smoke-device"})
    check("密码错与账号不存在返回同样的码与文案（不可枚举）",
          wrong.get("code") == missing.get("code")
          and wrong.get("message") == missing.get("message"),
          f"{wrong.get('code')} / {missing.get('code')}")

    st, forgot = call("POST", "/api/session/forgot", {"kind": "phone", "identifier": ident})
    dev_token = forgot.get("dev_token")
    check("遗忘密码可申请重置（dev 通道回显令牌）", st == 200 and bool(dev_token), f"status={st}")
    _, forgot_missing = call("POST", "/api/session/forgot",
                             {"kind": "phone", "identifier": "13900000000"})
    _, forgot_missing2 = call("POST", "/api/session/forgot",
                              {"kind": "phone", "identifier": "13900000000"})
    # 文案里嵌的是**入参的掩码**，它是入参的纯函数，与「账号是否存在」无关；
    # 结构上唯一的差别是 dev 通道的令牌（生产通道 dev_token_visible() 为 False）。
    check("未注册号与已注册号响应结构一致（不可枚举）",
          set(forgot_missing) == set(forgot) - {"dev_token"}
          and forgot_missing == forgot_missing2
          and "dev_token" not in forgot_missing,
          str(sorted(forgot_missing)))

    st, reset = call("POST", "/api/session/reset", {"token": dev_token, "new_password": pw3})
    check("凭令牌设置新密码", st == 200, f"status={st}")
    st, reuse = call("POST", "/api/session/reset",
                     {"token": dev_token, "new_password": "Pw-another-1"})
    check("重置令牌一次性（重放被拒）", st == 400 and reuse.get("code") == "invalid_token",
          str(reuse.get("code")))
    st, login3 = call("POST", "/api/session/login",
                      {"kind": "phone", "identifier": ident, "password": pw3,
                       "device_id": "smoke-device"})
    check("重置后的新密码可用", st == 200 and bool(login3.get("token")), f"status={st}")

    section("⑦ 访客越界：能力矩阵真的在拦")
    st, guest = call("POST", "/api/session/guest", {"device_id": "smoke-guest"})
    gtoken = guest.get("token") or guest.get("guest_token") or ""
    check("访客可签发", bool(gtoken), f"status={st}")
    st, _ = call("GET", "/api/models", token=gtoken)
    check("访客不能碰模型设置", st == 403, f"status={st}")
    st, _ = call("POST", "/api/location/recommend", {"lat": lat, "lon": lon}, token=gtoken)
    check("访客不能共享定位", st == 403, f"status={st}")

    section("⑧ 落盘检查：长期记录必须是密文")
    root = Path("data/dev/storage") if Path("data/dev/storage").exists() else Path("storage")
    files = list(root.rglob("*.json"))
    blob = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in files)
    check("账号库里没有明文手机号", ident not in blob, f"{len(files)} 个 json @ {root}")
    check("账号库里没有明文口令", not any(p in blob for p in (pw1, pw2, pw3)))

    bad = [r for r in RESULTS if not r[0]]
    print("\n%d/%d 通过" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("失败项：" + "；".join(r[1] for r in bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
