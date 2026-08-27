# applemusic-mcp

一个很小的 Apple Music MCP server：HTTP JSON-RPC 单文件实现，接入 Claude Code 等支持
`"type": "http"` 的 MCP 客户端后，AI 就能帮你搜歌、"点歌"（吐出一个卡片标记给你自己的
聊天前端渲染）、在你真实的 Apple Music 资料库里建歌单加歌，并自动维护一个"点过的歌"历史歌单。

**English**: A tiny single-file Apple Music MCP server (HTTP JSON-RPC). Wire it into Claude Code
or any MCP client that supports `"type": "http"`, and your assistant can search Apple Music,
emit a card-marker tag when it "plays" a song (for your own chat frontend to render), create/manage
real playlists in the user's Apple Music library via MusicKit, and auto-maintain a "history"
playlist of everything ever played. See below for setup, the pitfalls we hit building this
against Apple's APIs, and full credit to the project this was forked in spirit from.

---

## 来历与致谢

这个项目的架构和思路，全部来自无花果老师的网易云音乐 MCP：
**[Cheiineeey/netease-music-mcp](https://github.com/Cheiineeey/netease-music-mcp)**。

我们最早就是直接跑的那个仓库，用网易云给家里点歌。后来因为小鱼习惯用 Apple Music 而不是网易云，
才照着那个架构改成了现在这个 Apple Music 版本——HTTP JSON-RPC 的 server 形态、工具的拆法
（搜歌/点歌/建歌单/加歌/列歌单）、用一段特殊文本标记（`[xxx:...]`）让聊天前端识别出"这是一张
歌曲卡片"的思路，都是照搬过来的，只是把调用的 API 从网易云换成了 Apple Music，认证方式换成了
Apple 的 developer token + MusicKit user token 两层。

没有无花果老师那个仓库把整个"MCP 点歌"这件事的架子先立起来，我们不会这么快就有这个东西。
这里老老实实写清楚：**这不是"参考了"，是直接拿架构改的**。如果你也想搭一个类似的音乐点歌 MCP，
强烈建议先去看看原版。

---

## 功能清单

- `play_music(query, note?)` —— 搜索并"播放"一首歌，返回 `[amusic:id:歌名:歌手:封面url]note`
  格式的卡片标记文本（前端自己解析渲染，本仓库不含前端）
- `search_music(query, limit?)` —— 搜索 Apple Music 曲库，返回候选列表
- `create_playlist(name, description?)` —— 在用户真实的 Apple Music 资料库里建一个歌单
- `add_to_playlist(playlist_id, song_id, ...)` —— 往歌单里加歌
- `list_playlists()` —— 列出本 server 建过的歌单 + 资料库里的其它歌单
- 点歌历史：每次成功 `play_music` 都会记一条本地 JSONL 流水，并自动同步进一个真实的
  "历史歌单"（首次点歌自动建，之后逐首追加，同一首歌不会重复塞）
- 无 developer key / 无 user token 时优雅降级：搜歌能力不受影响，需要资料库写权限的工具
  返回友好的英文提示而不是崩溃

---

## 快速开始

### 1. 申请 MusicKit Key

Apple Music API 需要一把 **Media Services（MusicKit）Key**，流程有几个不直观的坑，见下面
"踩坑记录"，这里先给顺序对的步骤：

1. 登录 [Apple Developer 后台](https://developer.apple.com/account/resources/identifiers/list) →
   Certificates, Identifiers & Profiles
2. **先建一个 Media ID**（Identifiers → 点 `+` → 选 Media IDs）
   - 起名和填 identifier 时，**identifier 里不要包含 "music" 这个词**——它是保留词，会被拒，
     错误提示完全不会告诉你这一点（详见踩坑 ①）
3. 再去 Keys 页面新建一个 Key，勾选 **Media Services**
   - 如果这时候发现 Media Services 选项灰的、或者提示 "There are no identifiers available"，
     说明第 2 步的 Media ID 还没建好或者没通过审核（详见踩坑 ②）
4. 生成后下载 `.p8` 私钥文件（**只能下载一次**，丢了要重新生成一把新的），记下 Key ID
5. Team ID 在后台 Membership details 页面能看到（10 位字母数字）

### 2. 配置本仓库

```bash
cp .env.example .env
# 编辑 .env，填入 APPLE_TEAM_ID / APPLE_KEY_ID / APPLE_KEY_FILE
# 把第 1 步下载的 .p8 放到你指定的 APPLE_KEY_FILE 路径下
pip install pyjwt cryptography
python3 server.py
```

启动后 `curl http://127.0.0.1:3458/health` 应该返回 `{"status":"ok","dev_key":true,...}`。
`dev_key: false` 说明 Key 没配对，回去看第 1 步。

### 3. 拿到 Music User Token（资料库读写必须）

搜歌、`play_music` 不需要 user token，但建歌单/加歌/列歌单需要。这个 token 只能在客户端
（iOS app 或网页）通过 MusicKit 让用户完成一次授权后才能拿到——**本仓库不含客户端代码**，
你需要自己写一个（哪怕是几行的静态网页）来完成授权、拿到 token，然后把它写进
`DATA_DIR/user_token.json`，格式：

```json
{"token": "拿到的 Music User Token 字符串"}
```

最简单的网页版 MusicKit JS 大概长这样（完整文档见
[Apple MusicKit JS](https://developer.apple.com/documentation/musickitjs)）：

```js
await MusicKit.configure({
  developerToken: "<你的 developer token，可以用本 server 签一个>",
  app: { name: "My App", build: "1.0" }
});
const music = MusicKit.getInstance();
const userToken = await music.authorize();
// 把 userToken POST 给你自己的后端，写进 DATA_DIR/user_token.json
```

Swift（iOS 原生）版思路一致，走 `MusicAuthorization.request()` + `SKCloudServiceController`
或 MusicKit for Swift 的 `MusicAuthorization`，拿到授权后同样是把 token 落盘：

```swift
import MusicKit

let status = await MusicAuthorization.request()
if status == .authorized {
    // 用 MusicKit for Swift 发起一次请求以换取 user token，
    // 或走 SKCloudServiceController.requestUserToken(forDeveloperToken:)
    // 拿到 token 后 POST 给你自己的后端写入 user_token.json
}
```

---

## MCP 客户端接入示例

以 Claude Code 的 `.mcp.json` 为例（`type: "http"`，不是 `"sse"`，见踩坑 ④）：

```json
{
  "mcpServers": {
    "applemusic": {
      "type": "http",
      "url": "http://127.0.0.1:3458/message?token=YOUR_MCP_TOKEN"
    }
  }
}
```

启动 server 后，客户端应该能在 `tools/list` 里看到 `play_music` / `search_music` /
`create_playlist` / `add_to_playlist` / `list_playlists` 五个工具。

---

## 踩坑记录

这是这个仓库真正的价值所在——下面每一条都是我们排错好几轮才定位的，写下来希望你少走弯路。

**① Media ID 的 identifier 含 "music" 会被拒，报错完全不提示原因**
在 Apple Developer 后台新建 Media ID 时，如果 identifier（比如 `com.example.music`）里包含
"music" 这个词，会直接被拒，提示大概是 "not available" 之类的通用错误，**不会告诉你是因为
"music" 是保留词**。我们排错三轮才定位到是这个原因——换个不含 "music" 的 identifier（比如
`com.example.tunes`）就通过了。

**② Media Services Key 必须先建好 Media ID 才能勾选**
Keys 页面新建 Key 时，如果之前没有先建一个通过审核的 Media ID，Media Services 选项要么是灰的，
要么直接提示 "There are no identifiers available"。顺序一定是：先建 Media ID，等它可用了，
再去建 Key 勾 Media Services。

**③ Apple Music API 的 POST 响应是 gzip 压缩的，GET 不是**
用 `urllib` 直接对响应体 `json.loads()`，GET 请求没问题，但 POST 请求（建歌单、加歌）返回的
是 gzip 压缩过的内容，直接当 UTF-8 JSON 解析会报 `UnicodeDecodeError` 之类的编码炸裂。
要先检查响应体开头两个字节是不是 gzip magic number（`\x1f\x8b`），是的话先 `gzip.decompress`
再解析。本仓库的 `_api()` 函数已经处理了这个（见 server.py 里的注释）。

**④ 自制 HTTP 风格 MCP server，客户端要配 `type: "http"`，不是 `"sse"`**
这个 server 是纯 HTTP JSON-RPC（POST 一个 JSON body，同步返回一个 JSON response），不是
Server-Sent Events 流式协议。如果在 MCP 客户端配置里写成 `"type": "sse"`，现象是工具列表
永远加载中、永远超时，不会有明确报错——排查时先检查这一项配对没有。

**⑤ Apple Music API 不支持从歌单里删歌**
官方 API 没有对应的"移除歌单内某首歌"的端点。这意味着任何"同步"逻辑都要接受这个限制：
本仓库的历史歌单同步（`_sync_history_playlist`）设计成"宁可漏塞（跳过某次去重判断）也不重复
调用/报错"，而不是试图做一个完美的双向同步——因为万一状态文件和真实歌单内容对不上，
本地这边完全没有办法"回滚"或者"删掉多余的"。

**⑥ user token 是绑 Team 的，不是绑具体某个 developer token**
同一个 Apple Developer Team 下，不同的 developer token（不同 Key 签的、甚至不同时间签的）
都可以配合同一个 Music User Token 使用——user token 认的是 Team，不是某一次签名。这意味着
你换 Key、重新签 developer token 都不需要用户重新走一遍 MusicKit 授权。

**⑦ Apple 不开放歌词数据**
Apple Music API 出于版权原因不提供歌词接口。如果你的场景需要歌词，本仓库帮不了你，得自己
从别的渠道（比如网易云歌词接口、第三方歌词库）另外接。

---

## 关于卡片标记 `[amusic:...]`

`play_music` 返回的不是"已经在播放"的确认，而是一段文本约定：

```
[amusic:<catalog_song_id>:<歌名>:<歌手>:<封面url>]<可选备注>
```

这是给**你自己的聊天前端**用的——前端拿到助手回复后，用正则识别这个前缀，抽出字段渲染成一张
带封面、能点开在 Apple Music 里播放的卡片，而不是把这段方括号文本原样展示给用户看。本仓库
只负责生成这段文本，不含任何前端渲染代码，也不含"这段文本具体怎么接进某个聊天系统"的细节——
那部分是你自己项目的事。

---

## 环境变量一览

见 [`.env.example`](.env.example)，逐项带注释。核心几个：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `APPLE_TEAM_ID` | Apple Developer Team ID | 无，必填 |
| `APPLE_KEY_ID` | MusicKit Key 的 Key ID | 无，必填 |
| `APPLE_KEY_FILE` | `.p8` 私钥文件路径 | 无，必填 |
| `APPLE_STOREFRONT` | Apple Music storefront（`us`/`cn`/...） | `us` |
| `MCP_TOKEN` | 客户端 URL 里的 `?token=` 值（server 暂不校验，见 server.py 注释） | 未设置时每次启动随机生成 |
| `DATA_DIR` | 数据文件（user_token/歌单账本/历史）存放目录 | `./data` |
| `HISTORY_PLAYLIST_NAME` / `HISTORY_PLAYLIST_DESC` | 自动维护的历史歌单名称/描述 | `Songs picked for you` / `Every song picked for you` |

## License

MIT，见 [LICENSE](LICENSE)。再次感谢 [Cheiineeey/netease-music-mcp](https://github.com/Cheiineeey/netease-music-mcp) 的原始架构。
