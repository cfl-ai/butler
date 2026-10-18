# MEMORY.md

> 依据：《AI管家Agent生态体系项目计划书》三、P10
> 「搭建 MEMORY.md 持久记忆沉淀机制，存储缺陷与解决方案；不存储真实用户隐私，
> 记录 Jev 失败 case、Wiki 典型矛盾案例」
>
> **存储红线**：本文件不存储任何真实用户隐私，不含明文手机号、账号、银行卡、完整位置。
> 仅记录工程缺陷、环境约束与对应解决方案。

---

## 一、环境缺陷与解决方案（本机实测）

### E-01　DSH 沙箱禁止连接 Docker 命名管道

- **现象**：普通沙箱下执行任意 `docker` 子命令（info / ps / images / compose）均返回
  `permission denied while trying to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`，
  工具结果中沙箱标记 `denied: true`。
- **机制**：受限令牌不能打开命名管道（DSH 已文档化的边界）。该限制与 Docker 本身无关——
  同一沙箱下自建命名管道、TCP 监听、TCP 连接均实测正常。
- **影响**：Docker CLI 在普通沙箱下完全不可用。
- **解决方案**：**容器生命周期操作走一次性提权；应用侧一律走 localhost TCP，普通沙箱即可。**
  本机端口分配：PostgreSQL `5434`、Redis `6381`；不与 job-agent（`5433` / `6380`）冲突。

### E-02　Docker Desktop 启动需要提权

- **现象**：普通沙箱下启动 Docker Desktop，后端进程立即退出，当日日志无任何新增记录。
- **机制**：`com.docker.backend.exe` 需写入 `%LOCALAPPDATA%\Docker\log\host`，该路径在工作区之外被沙箱拒绝：
  `unable to create a log file for com.docker.backend.exe in directory ... Access is denied.`
- **解决方案**：启动 Docker Desktop 时使用一次性提权。daemon 启动后常驻，应用侧无需再提权；
  重新开机后需再执行一次。

### E-03　`tempfile.mkdtemp` 目录在受限沙箱内不可写、亦不可删

- **现象**：`tempfile.mkdtemp()` 创建的目录，任何进程在其中写文件均报
  `PermissionError [Errno 13] Permission denied`；该目录本身也无法被删除。
  `os.makedirs()` 创建的目录**不受影响**，可正常读写。
- **机制**：此类目录的 Windows 文件权限项只含「所有者权利 / SYSTEM / Administrators」三项，
  受限令牌下当前用户的有效权限实测为空（`MY_RIGHTS=[]`），故无法写入。
- **影响**：`ensurepip` 与 `pip` 内部依赖临时目录 → **venv 无法在普通沙箱内创建**。
- **已排除**：`diagnose-windows-sandbox-acl` 判定为 `NOT_THIS_CLASS`——
  权限项健康，`WRITE_DAC` 与 `WRITE_OWNER` 均可用，无需修复，未做任何改动。
  报告存档：`.acl-recovery/acl-report-*.jsonl`
- **解决方案**：**创建 venv 与安装依赖使用一次性提权执行；创建完成后，运行与测试全部在普通沙箱内进行。**

### E-04　外网可达性差异（影响依赖与模型获取）

| 目标 | 结果 |
|---|---|
| pypi.org / pypi.tuna.tsinghua.edu.cn | 可达（安装依赖走清华镜像） |
| api.deepseek.com | 可达 |
| hf-mirror.com / modelscope.cn | 可达（模型权重走此源） |
| huggingface.co | 超时 |
| github.com / registry-1.docker.io | 超时（Docker 镜像走 `docker.m.daocloud.io`） |

### E-05　本机无移动端工具链（P7 前置约束）

- **事实**：本机无 Flutter、无 Android SDK；`java` 命令不存在。
- **影响**：P7 的「独立手机 App（纯客户端、非网页）」在本机 **无法产出 APK**，
  任何端侧验证（唤醒、VAD、ASR、TTS）都做不了。
- **解决方案**：先用 Python 在 Windows 上把「唤醒 → VAD → 流式 ASR → LLM → TTS」
  整条链路跑通并录样本，再决定是否安装 Flutter / Android SDK 出 App。
  端侧引擎已裁定为 sherpa-onnx（唤醒/ASR/TTS 同栈）+ silero-vad，Python 侧即可验证。
- **相关裁定**：见 `ai_butler/core/decisions.py` 的 U05 / U06 / U20 / U21 / U22。

### E-06　Docker Desktop 不代理绑定到 127.0.0.1 的容器端口

- **现象**：compose 里写 `127.0.0.1:5434:5432` 时，容器 `HostConfig.PortBindings`
  里能看到 `{"5432/tcp":[{"HostIp":"127.0.0.1","HostPort":"5434"}]}`，
  但 `docker ps` 的 PORTS 列只显示 `5432/tcp`，宿主上没有任何监听，连接直接 REFUSED。
- **机制**：本机 Docker Desktop 使用 WSL2 后端，其端口代理不为回环绑定的容器端口创建宿主监听。
- **解决方案**：本地环境改绑 `0.0.0.0`（与 job-agent / datamind 一致）。
  风险由「只有 dev / test 叠加 `local-ports.yml`，预发布与生产不发布任何宿主端口」控制，
  见 `ai_butler/core/environments.py` 的 `exposes_host_ports` 与 `tests/test_network_zones.py`。

### E-07　Docker 不为「只挂 internal 网络」的容器发布端口

- **现象**：容器只挂在 `internal: true` 的网络时，`HostConfig.PortBindings` 有值，
  但 `NetworkSettings.Ports` 为 `{"5432/tcp":[]}`（未发布），宿主连接 REFUSED。
  后端日志只出现 `exposer.Add(..., -> 127.0.0.1:0)`，**缺少**带真实容器 IP 的第二条 Add。
- **对照实验**：同镜像同端口，普通网络 → `{"5432/tcp":[{"HostIp":"0.0.0.0","HostPort":"15436"}]}` 通过；
  `--internal` 网络 → `{"5432/tcp":[]}` 失败。已排除 Windows 端口保留段
  （`netsh` 排除段不含这些端口）与 Docker Desktop 代理卡死（重启无效）。
- **解决方案**：模板把内网服务层的 `internal` 写成环境驱动 `${DATA_PLANE_INTERNAL:-true}`：
  **预发布/生产保持 true 且不发布任何宿主端口；本地 dev/test 设为 false**，以便宿主可达数据面。
  **沙箱隔离层不受该开关影响，任何环境都严格 internal**（由测试断言）。
- **相关**：`ai_butler/core/environments.py` 的 `data_plane_internal`、
  `deploy/compose/base.yml`、`tests/test_network_zones.py`。

## E-08 测试用事件循环夹具只 new 不 set，导致整包连跑偶发失败

- **现象**：单独跑某个测试文件全绿；**整包连跑**时偶发
  `ERROR at setup of test_... - asyncio...`，集中在需要 asyncpg 的模块
  （最典型是 `tests/test_rag_materials.py`）。重跑往往就好了，极具迷惑性。
- **机制**：同一 pytest 进程内，若前面的用例调用了 `asyncio.run()`，
  它结束时会把**当前事件循环置空**（`Runner` 的清理会 `set_event_loop(None)`）。
  此后模块级的 `loop` 夹具若只 `asyncio.new_event_loop()` 而不 `set_event_loop(lp)`，
  该循环就只是「一个对象」，不是「当前循环」；asyncpg / SQLAlchemy 的某些代码路径
  会去取当前循环，于是连接挂在了一个已关闭或不对应的循环上。
- **解决方案**：所有模块级 `loop` 夹具改为
  `new_event_loop()` → `set_event_loop(lp)` → 用毕 `close()` + `set_event_loop(None)`。
  已加固 7 个文件（`test_db` / `test_data_platform_*` / `test_rag_materials` /
  `test_scheduler_*`）；加固后连续 3 次整包连跑零失败。
- **教训**：**「重跑就好了」不是通过，是还没定位。** 这类偶发必须当成缺陷处理。

## E-09 Flutter / Dart 工具链在工作区沙箱下必须改 PUB_CACHE

- **现象**：首次运行 `flutter --version` 反复失败并重试：
  `Creation failed, path = 'C:\Users\<用户>\AppData\Local\Pub' (OS Error: 拒绝访问。, errno = 5)`。
- **机制**：pub 默认把包缓存放在 `%LOCALAPPDATA%\Pub`，而该路径**在工作区之外**，
  被 DSH 文件沙箱拒绝；flutter 工具自身也由 pub 引导，于是陷入重试循环。
- **解决方案**：把 pub 缓存与 flutter 的 home 都指到工作区内再运行：
  ```
  PUB_CACHE=<工作区>/.toolchains/pub-cache
  APPDATA / LOCALAPPDATA=<工作区>/.toolchains/flutter-home   （兜底）
  FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
  PUB_HOSTED_URL=https://pub.flutter-io.cn
  ```
- **SDK 获取**：Flutter 3.47.7 稳定版 Windows 归档 1845 MB，走 `storage.flutter-io.cn` 约 100 秒；
  解压用 Windows 自带 `tar -xf`（bsdtar），比 Expand-Archive 快一个量级。
- **附带教训**：**PowerShell 的 `Invoke-WebRequest` 在本机会对若干 HTTPS 站点误报
  「基础连接已经关闭」**，而同样的 URL 用 httpx / curl 全部 200。
  遇到网络失败先用第二个客户端复核，别据此判定「不可达」。

## E-10 P7 端侧语音：引擎层与推理层均已验通（**根因是测试数据不对应，不是引擎不兼容**）

- **已证实**（`scripts/verify_voice_engine.py`，引擎层 4/4 PASS + 推理层 PASS）：
  - sherpa-onnx 1.13.8 Windows CPU 轮子可装可用；
    `KeywordSpotter` / `OnlineRecognizer` / `OfflineTts` / `VadModel`
    与裁定 U06/U20/U21/U22 一一对应；
  - U06 裁定的模型可从 modelscope 获取；int8 三件套 **4.82 MB**，与裁定「约 4.9MB」吻合；
  - **真实检出**：ASR 转写 0.wav 成功，再用转写结果构造的 4 条关键词
    （介绍 / 感兴趣 / 我想说的是 / 大家）**全部被 KWS 正确检出**；
  - 「你好管家」拆为 10 个 token，全部在 tokens.txt 内。
- **根因（绕了很久才查到）**：模型仓里的 `test_wavs/test_keywords.txt`
  **与同目录的音频并不对应** —— 用同一套模型当普通 ASR 转写 0.wav 得到的是
  「对我做了介绍么我想说的是大家如果对我点酒感兴趣」，
  里面**根本没有** test_keywords.txt 列的那些词（文森特卡索、周望军…）。
  拿它做关键词表自然一个都检不出，**极易被误判成「引擎不兼容」**。
  正确做法：**先转写音频，再从转写结果里取确实存在的短语构造关键词**。
- **关键词 token 规则**：每个音节要拆成「**声母 + 韵母**」两个 token
  （介绍 = `j` + `iè` + `sh` + `ào`），不是整音节一个 token；
  token 不在 tokens.txt 里时引擎打印 `Cannot find ID for token`。
- **两个 KWS 用法必须照做**：
  1. **结果要在解码循环内逐块读**，循环结束后再读只会拿到空串；
  2. 检出后要 `reset_stream`，否则同一条流不会报第二次。
- **API 形状不要凭印象猜**：本脚本连着写错三处——
  `read_wave` 不存在（只有 `write_wave`）、`get_result` 直接返回字符串、
  `OnlineRecognizerConfig` 与 `OfflineTtsVitsModelConfig.sample_rate` 都不存在。
  每次都是「跑一遍才发现」。
- **装机绕路**：本机 pip 装 sherpa-onnx 会长时间无输出（国际带宽，**不是死锁**：
  2.2 MB 的 wheel 下了 165 秒）。可行做法是用 httpx 从 pypi.org 的 JSON 取
  files.pythonhosted.org 直链下载 wheel，再 `pip install --no-index --no-deps <wheel>`。
  注意 `--no-deps` 会漏掉 **sherpa-onnx-core（16 MB）**，
  漏了它模型加载会报 `The given version [28] is not supported`。

## E-11 Flutter 工具链的根因是**沙箱禁止管道捕获子进程输出**

- **现象**：`flutter --version` 反复重试并报
  `Error: Flutter failed to run "git -c log.showSignature=false log -n 1 --pretty=format:%H"`，
  伴随 `CreateFile failed 5（拒绝访问）`。
- **误判过程**：先后以为是「缺 git」「写不了 %APPDATA%」「pub 缓存在工作区外」，
  逐个改了都没解决；中途还累积了 **327 个 41.5MB 的 flutter_tools.snapshot.old\*（13.26 GB）**。
- **真相**：git 装了（2.55.0）、SDK 有 .git、同一条 git 命令**单独运行完全成功**。
  差别在于 flutter 是**用管道捕获 git 输出**的 —— 而沙箱禁止程序开管道捕获子进程输出。
- **解法**：对 flutter 命令使用一次性提权（`sandbox_permissions: "danger-full-access"`）。
  随后 `flutter --version` 一次通过：
  `Flutter 3.47.7 • channel stable • Tools • Dart 3.13.5`。
- **同类记录**：`dart test` 当初也需要提权，原因是同一个（子进程 + 管道）。
  **以后遇到「子进程相关」的沙箱拒绝，先想到管道这一条。**

## E-12（已作废）sherpa_onnx Dart 绑定结构体不匹配 —— **这个诊断是错的，见 E-13**

曾判断「pub 的 Dart 绑定 FFI 结构体与 C 库字段顺序不一致」，**该结论已被推翻**：
按下面的 E-13 预加载 onnxruntime 后，**pub 的 `package:sherpa_onnx` 构建完全正常**。
把这条保留下来是为了记住一个教训：
**在排除法里，我把「配置打印正确但模型加载崩」直接归因到了绑定库，而没有先怀疑 DLL 解析。**

## E-13 **Windows 的 System32 里躺着一个上古 onnxruntime.dll（1.10），会抢先被加载**

- **症状**：sherpa-onnx 在 Dart 里加载模型时报
  `The given version [28] is not supported, only version 1 to 10 is supported in this build.`
  随后**访问冲突崩溃**（`ExceptionCode=-1073741819`）。
  报错里的「1 to 10」不是巧合 —— 那就是 **ORT 1.10**。
- **根因**：Windows 解析 DLL 依赖的顺序是「**exe 目录 → System32 → PATH**」，
  **DLL 自己所在目录并不优先**。而
  `C:\Windows\System32\onnxruntime.dll` 是 Windows 自带的 **1.10.220126**（Windows ML 用的）。
  它抢先生效，sherpa-onnx 拿到的 ORT API 是 10，而它需要 28。
- **证据链**：
  1. `Get-ChildItem C:\Windows\System32 -Filter onnxruntime*.dll`
     → `1.10.220126-2359.1.dml`；
  2. 用 **ctypes**（且 `os.add_dll_directory` 指向正确目录）调同一个
     `sherpa-onnx-c-api.dll`，**创建引擎成功** —— 说明 DLL 与结构体都没问题；
  3. Dart 把正确目录加进 PATH 也没用（System32 优先于 PATH）；
  4. **改成先用绝对路径 `DynamicLibrary.open(onnxruntime.dll)` 预加载 → 一次通过**，
     而且此时 PATH 里**故意不放** dll 目录。
- **解法**（已落到 `aibutler_voice` 的 `preloadNativeLibraries`）：
  `dart
  DynamicLibrary.open(dllDir + sep + 'onnxruntime.dll');   // 先依赖
  DynamicLibrary.open(dllDir + sep + 'sherpa-onnx-c-api.dll');  // 后被依赖者
  sherpa.initBindings();
  `
  **顺序不能反**。已加载模块按基名复用，后续依赖解析不再去 System32 找。
- **同类风险**：任何依赖 `onnxruntime.dll` 的原生库在 Windows 上都可能踩这个坑。
  本机 `System32` 之外的 `onnxruntime.dll` 还有 Edge / Office / WSL 带的一堆
  （1.19 / 1.23 / 1.24 / 1.29），版本各不相同。
- **连带教训**：排查时我用「字符串搜索 DLL 里有没有 `wenet_ctc`」当作结构体布局证据 ——
  **那个实验无效**（搜到的是字符串值不是字段名）。后来改用
  **ctypes 直接调 DLL** 才把「库的问题」与「调用方的问题」干净分开。
  **能把问题一分为二的实验，比任何推理都值钱。**

## E-14 系统临时目录不可写会让 **chromadb 整片报错**，极易被误读成「代码坏了」

- **症状**：`tests/test_rag_materials.py` 等一批 RAG 用例集体 ERROR：
  `chromadb.errors.InternalError: error returned from database: (code: 14) unable to open database file`。
- **根因**：`C:\Users\<用户>\AppData\Local\Temp` 在该沙箱模式下**不可写**（`PermissionError`）。
  Python 于是**静默降级**把临时目录落到当前目录，而 chromadb 的 rust 绑定要自己在临时目录建库 ——
  建不了就报上面那句，**与业务代码毫无关系**。
- **一个把我带偏的细节**：`tempfile.gettempdir()` 在该目录不可用时**会返回当前目录**。
  所以「探一下 gettempdir() 能不能写」这种自愈逻辑**必然通过、等于没探**。
  正确做法是直接探**环境变量 `TMP` / `TEMP` 指向的路径**。
- **解法**（已落在 `tests/conftest.py` 的 `_ensure_writable_temp`）：
  导入任何被测模块之前先探环境变量指向的临时目录；不可写就把
  `TMP` / `TEMP` / `TMPDIR` 指到工作区内的 `.pytest_tmp`，并设 `tempfile.tempdir`。
  这样 pytest 在任何沙箱模式下都能跑。
- **排查顺序上的教训**：看到「一整片用例连同报数据库错误」，**先怀疑环境（临时目录、文件句柄），再怀疑代码** ——
  否则会去翻一堆本来没问题的业务逻辑。

## E-15 工具链在本机崩，**要先分清是「中文路径」还是「文件策略」**

这两个原因的现象很像（都是「找不到路径 / 拒绝访问」），但**解法完全不同**。
本轮我先后把它们搞混过一次，最后靠一个**让 Java 自己报告**的实验才分清。

### 实验：直接问 Java「你能写哪里」

用一个单文件 Java 程序逐一试写（源码里用 `\u9648\u5bcc\u6797` 转义，
避免 `javac` 按 GBK 读源码把中文字符串搞坏）：

| Java 能写？ | 路径 | 说明 |
|---|---|---|
| **FAIL** `AccessDenied` | `C:\Users\陈富林\.gradle` | **工作区之外** |
| OK | `C:\Users\陈富林\PycharmProjects\.toolchains\gradle-home` | 工作区内（**路径照样有中文**） |
| **FAIL** `AccessDenied` | `D:\ab-gradle` | **工作区之外** |
| OK | `D:\ab-build` | **目录联接，解析回工作区** |

**结论：判据是「在不在工作区内」，与中文无关。**
Java 的 `file.encoding=UTF-8` 正常，写中文路径也正常。

### 于是两个问题的正确解法是

| 现象 | 真因 | 解法 |
|---|---|---|
| Gradle 报 `FileNotFoundException: ...\.gradle\wrapper\...lck` | `GRADLE_USER_HOME` 默认在 `%USERPROFILE%\.gradle`，**在工作区外**被拒 | 把 `GRADLE_USER_HOME` 指到**工作区内**（如 `\.toolchains\gradle-home`） |
| `flutter test` 报 `ShaderCompilerException`，`#include "flutter/runtime_effect.glsl"` 找不到 | **真的是中文路径**：报错里是 `%EF%BF%BD`（U+FFFD 替换字符），说明路径被**实际损坏**了，不只是控制台乱码 | 用 **ASCII 路径的目录联接**指向工程（`D:\ab-build`）再构建 |

- **一个必须记住的分辨方法**：控制台里的中文经常显示成乱码（本机一直如此），
  所以**不能**靠肉眼看日志判断路径有没有被损坏。
  要看的是报错里出现的是**残留的中文**还是 `%EF%BF%BD` / `?` 这类**替换字符** ——
  后者才说明路径真的坏了。
- **8.3 短名这条路走不通**：本机 `陈富林` 这个目录**没有生成 8.3 短名**，
  所以 `%~sI` 拿到的仍是中文（`C:\Users\陈富林\PYCHAR~1`）。

## E-16 Android 工具链从零搭起来（以及工具本身不可靠时怎么办）

本机原本**没有 JDK、也没有 Android SDK**。踩到的坑与绕法：

| 环节 | 问题 | 绕法 |
|---|---|---|
| JDK 17 | 清华 Adoptium 镜像 **对 GET 返回 403**（同 URL 的 HEAD 却是 200，属反盗链） | 改用**微软 OpenJDK**（`aka.ms/download-jdk/microsoft-jdk-17-windows-x64.zip`，178 MB / 7 秒） |
| Android cmdline-tools | 无 | dl.google.com 直下（146 MB） |
| `sdkmanager` 装组件 | 报 `IO exception while downloading manifest` / `Failed to find package`，**但同样的 URL 用 httpx 全部 200** | **绕开 sdkmanager**：直接下官方 zip 自己解压摆放（`platform-36_r01.zip`、`build-tools_r34-windows.zip`、`platform-tools-latest-windows.zip`） |
| Gradle 发行包 | 走 `services.gradle.org` 慢 | wrapper 换**腾讯镜像** `mirrors.cloud.tencent.com/gradle/`（224 MB，实测 200） |
| Gradle 依赖仓库 | **`maven.google.com` 实测超时** | `settings.gradle.kts` / `build.gradle.kts` 里让**阿里云镜像优先** |

> **一条通用经验**：`sdkmanager` 干的其实就是「下载官方 zip + 解压摆放」。
> 工具网络不通时，用脚本自己摆一遍是**完全等价**的 —— 不要卡在工具上。
> 同理，任何「下载器」类工具失败时，先确认它要下的 URL 本身通不通。

## E-17 **sdk.dir 里的中文路径会毁掉 AGP 生成的 JSON** —— APK 构建失败的真凶

- **症状**：`flutter build apk` 报 `Execution failed for task :jni:buildCMakeDebug[arm64-v8a]`，
  异常是 `com.google.gson.stream.MalformedJsonException: Invalid escape sequence at line 7 column 26 path $.cleanCommandsComponents[0][0]`。
- **误导性**：它长得像「AGP 的 CMake 集成有 bug」，很容易就此收手。
  我一度也按「第三方插件工具链问题」处理，还为此下了 NDK（713 MB）与 CMake。
- **真相**：AGP 生成的 `android_gradle_build.json` **本身就是非法 JSON** ——
  里面的 ninja 路径写着被毁坏的中文用户名，产生了非法的转义序列，Gson 解析即抛错。
  证据在 `build/jni/intermediates/cxx/.../logs/<abi>/metadata_generation_record.json`，
  它直接写着 "JSON generation completed with problem. Exception: ..."。
- **路径从哪来**：**`android/local.properties` 的 `sdk.dir`**。
  它由 Flutter 依据 `flutter config --android-sdk` 写出，而那次配置指向了带中文的真实路径。
- **解法（三步缺一不可）**：
  1. 把 Android SDK 放到**真正的 ASCII 目录**（`D:/android-sdk`）——
     **目录联接不够**，AGP 会把联接解析回带中文的真实路径；
  2. `flutter config --android-sdk D:/android-sdk`（配置持久化到 `<APPDATA>/.flutter_settings`）；
  3. 删掉 `android/local.properties` 让其按新配置重写，构建前清掉 `build/` 与 pub 缓存里的 `.cxx`。
- **验证结果**：release 分 ABI 产物 `arm64-v8a 42.7 MB` / `armeabi-v7a 32.1 MB` / `x86_64 47.6 MB`，
  debug 227.5 MB。release APK 内含 `libapp.so`（Dart AOT 快照）、`libsherpa-onnx-c-api.so`、
  `libonnxruntime.so`、`libdartjni.so`，以及 AndroidManifest.xml / classes.dex / resources.arsc。
- **教训（与 E-15 同源）**：这是本项目**第三次**被中文路径咬到
  （着色器编译器、sdk.dir 导致 AGP JSON 损坏、Gradle 缓存）。
  **遇到「工具在本机莫名失败、报的又像是别人的 bug」，先去看它内部生成的中间文件是不是被写坏了。**
  这次若只看 Gradle 的报错信息，就会把它当成 AGP 缺陷而绕道走。

## E-18 **「能构建出 APK」不等于「能在真机上跑」**

第 33 节刚把 APK 构建打通时，很容易就此认为 P7 的客户端已经就绪。
用 `aapt2 dump badging` 一看清单才发现：**整份清单只有一条权限**（INTERNET）。
那个 APK 装到手机上，语音功能一点都用不了 —— 没有 RECORD_AUDIO，
而且 `RecorderMicrophone.start()` 当时是直接抛「需要录音插件」的。
- **教训**：构建成功只说明**编译链**通了。
  「装上去能不能用」要另外查：权限、服务声明、以及那条能力**是否真的有实现**。
  后来把这三件事都补齐，并把「声明层」纳入机检（scripts/verify_android_artifact.py）。

## E-19 XML 注释里不能出现连续两个连字符

- **症状**：`flutter build apk` 报
  `ManifestMerger2$MergeFailureException: Error parsing AndroidManifest.xml`，
  但错误信息**不告诉你是哪一行**。
- **原因**：我在清单注释里画了分隔线（一长串连字符），而 XML 规范不允许注释内出现 `--`。
- **排查办法**：别盯着 Gradle 的输出看，**直接用 XML 解析器解一遍**，
  它会把行列号指出来：
  ```python
  import xml.etree.ElementTree as ET
  ET.fromstring(open("AndroidManifest.xml", encoding="utf-8").read())
  ```
- **同类提醒**：Android 清单、strings.xml、以及任何 XML 资源，
  **注释里都不要写连续连字符**，也不要写 `--` 结尾的句子。

## E-20 Android 模拟器在本机不可用（已实测，不必再试）

想用模拟器替代真机回答一部分 U23 的问题，两条硬门槛都过不去：

| 门槛 | 实测 |
|---|---|
| 硬件加速 | `HypervisorPresent: True`（已有 hypervisor 在跑，所以 CPU 能力位反而报 False），但 **WHPX（HypervisorPlatform）功能未启用**，启用要重启系统 |
| 内存 | 可用仅 **2.25 GB**，模拟器本身要 2–4 GB |

结论：**本机无法用模拟器替代真机**。真机层只能等真机。

## E-21 **「声明齐了」之后还要问一句：那条能力真的有实现吗**

第 34 节把权限、前台服务、录音适配器都补齐了，验收脚本也全绿。
但再往下问一层就露馅了：

| 空洞 | 后果 |
|---|---|
| 唤醒词模型**没有随包** | 引擎拿不到模型，装到真机上也唤不醒 |
| 前台服务的桥建好了却**没接进流程** | 服务永远不会被拉起，只停在声明上 |
| 录音端口是真实实现，但**没接到唤醒引擎** | 采到的采样没人用 |

- **教训**：把「权限/声明」当成「能力」是一种很隐蔽的自欺。
  声明只说明「有资格做」，不说明「真的做了」。
  验收脚本覆盖到哪一层，就要说清楚是哪一层 —— 所以它现在明确分成
  **声明层（本脚本）** 与 **运行时层（只有真机）** 两段来输出。

## E-22 模型仓自带的关键词文件**含别家产品的商标**，不能随包下发

- 本项目的 KWS 模型来自 modelscope 的 `pkufool/sherpa-onnx-kws-zipformer-wenetspeech-3.3M`。
  它自带的 `keywords.txt` 里是：`你好军哥 / 蛋哥蛋哥 / 小爱同学 / 你好问问 / 小艺小艺 / 小米小米`。
- **其中「小爱同学」「小艺小艺」「小米小米」是别家产品的唤醒词与商标**。
  顺手把模型仓那份拷进 App 是很自然的动作，**但那是会出事的那种自然**。
- 本项目的唤醒词取自**裁定 U06**（台账登记的，不是自己定的）：` n ǐ h ǎo g u ǎn j i ā @你好管家`。
- **已把它变成机检**：`scripts/verify_android_artifact.py` 会从 APK 里
  把那几个词搜一遍，出现即判失败。防的不是「写错」，而是「有人顺手拷进来」。

## E-23 assets 不能直接当文件路径用

- sherpa-onnx 要的是**文件路径**，而 assets 在 APK 里是**压缩资源**，没有可用的文件系统路径。
- 做法：`lib/adapters/asset_models.dart` 首次启动把模型释放到应用私有目录；
  之后按「文件存在且大小一致」跳过，不重复写那 4.8 MB。
- 释放是**异步**的，而引擎是在 `initState` / `build` 期间构造的，等不了 ——
  所以给适配器加了 `modelResolver`：**等真正 start() 时再解析路径**，
  而不是在构造期硬取（那样只能拿到 null 或者阻塞 UI）。

## E-24 **只写在文档里的约束等于没有约束**

- 用户的硬约束是「md 产出仅限计划书要求的文档」（六类）。
- 但 `flutter create` 在 `mobile/` 下顺手生成了一个模板 `README.md`，
  **过了好几轮都没人发现** —— 因为没有任何机检在看这件事。
- 本轮补了 `tests/test_docs_whitelist.py`，并且**验证过它会红**：
  放一个诱饵 README 进去，两条用例立刻失败；移除后恢复。
- **教训**：约束要落到「会让它变红的东西」上。
  写进规范文档只是记录，写进测试才是约束。
  顺带一提，**一个从没失败过的守护测试，你并不知道它到底在不在工作** ——
  故意造一次违规、看它红，是给守护本身做的验收。

## E-25 脚手架工具会往项目里塞白名单外的文件

除了 `README.md`，`flutter create` / `dart run` 还会留下：

| 文件 | 处理 |
|---|---|
| `mobile/README.md` | 模板样板文，**不在白名单**，已删 |
| `mobile/flutter_*.log` | 崩溃日志（着色器编译失败那两次留下的），已删 |
| `.metadata` / `*.iml` / `.gitignore` / `analysis_options.yaml` | 构建脚手架，保留 |

- 引入任何脚手架工具之后，**顺手看一眼它新增了什么**。
  多数是必要的脚手架，但偶尔会夹带一份模板文档 —— 而模板文档往往正好踩在约束上。

## E-26 CI 里「安装依赖」排在「用到依赖的步骤」之后 —— 第一次运行就会失败

- `.gitea/workflows/ci.yml` 生成 `.env.test` 的那一步 `from cryptography.fernet import Fernet`，
  而 `pip install -r requirements.txt` 整一步排在**更后面**。
- shell 顺序执行，所以 `python` 先跑、依赖还没装 —— **CI 第一次运行就 ImportError**。
- 而 CI 平时不跑、没人看，这种错误可以一直躺着不被发现。
- **通则**：在 `run: |` 的多行脚本里，**别在用到依赖的步骤之前才装依赖**；
  装依赖应当是流水线的早期独立步骤。

## E-27 **守护测试是绿的，不等于守护有效**

给 CI 加了守护测试后，我把它要防的那个 bug **原样放回去**验证 —— 结果 **5 passed，没抓住**。
- 原因：CI 里是 `from cryptography.fernet import Fernet`，
  而我的判据只写了 `f"import {name}"`，**认不出 `from X import ...` 这种写法**。
- 修好判据后，同一个 bug 立刻被精确定位到「第 3 步 vs 第 4 步」。
- **通则**：写完守护，**故意造一次违规看它红**。
  「没报错」可能只是因为它根本没在检查 —— 与 E-24 同源，
  但这次连守护自己都犯了同样的毛病，值得单独记一条。

## E-28 CI 是项目里最容易悄悄腐坏的东西

CI 只写在配置里、平时不跑，**新加的验证入口不会自动进去**。
本项目陆续加了 P0 冒烟、语音引擎验证、Dart 核心测试，原来的 CI **一条都没覆盖**。
- 已经做的：补齐 job 与步骤；并加 `tests/test_ci_pipeline.py`，
  把「必需的验证入口」列成清单 —— **以后再加验证脚本，忘了进 CI 就会变红**。

## E-29 「声明 ≠ 能力」发生在本项目自己的代码里

- `p0_devkit.py` 的 digest 里，能力面是**写死的字符串清单**：
  声称有 `scaffold / testkit / prompt_registry / scenario_library / evaluation / log_query` 六项，
  但其中三项**从没被 import 过** —— 模块被改名或删掉，digest 依然报「有」。
- 同一个 digest 里的 `case_total` / `scenario_categories` 都是真算的，唯独能力面是手抄的，
  这个反差本身就是线索：**同一份输出里，能算的别写。**
- 已改为 `_probe_capabilities()` 逐个 import，并同时报出 `capabilities_missing`。
- **通则**：凡是「我支持 X / Y / Z」这样的清单，只要可以推导或实测，就不要手写。
  手写的清单会随着代码一起腐烂，而且**烂了不报错**。

## E-30 守护要连自己一起验收

给 `_probe_capabilities()` 写守护时，特意加了一条
`test_probe_really_detects_a_missing_module`：用 monkeypatch 塞一个不存在的模块，
断言它落进 `missing`。
- 没有这一条，另外几条测试无法证明探测在工作 ——
  **一个永远返回「全都在」的实现同样能让它们变绿。**
- 这已经是本项目**第三次**遇到同类问题（E-24 md 白名单、E-27 CI 顺序守护、本条）。
  现在把它固化成习惯：**写完守护，先想「它怎么才会红」，然后真的让它红一次。**

## E-31 「能推导的就别手写」—— 同一份输出里的反差就是线索

- P9 的评估报告报脱敏两层：`model_layer` 是**端口注入推导**的，
  而它旁边的 `regex_layer` 直接**写死 True**。
  **同一个字典里，一个推导、一个手抄** —— 这个反差本身就是最好的线索。
- 修法与 P0-2 同构：给 `Redactor` 补一个与 `model_layer_active` 对称的
  `regex_layer_active`（`bool(self._compiled)`），Agent 读属性而不是写常量。
- **排查方法（可复用）**：看一份 digest／报告时，逐个字段问
  「它是 import 来的、算出来的，还是手抄的？」
  手抄的那些就是会腐烂的那些。

## E-32 守护要能区分「默认对」与「推导对」

- 原有的 `test_agent_reports_redaction_layers` 只断言默认是 `True` ——
  **恒返回 True 的实现同样能过**，它测的是「默认值对不对」，不是「有没有推导」。
- 补法：把规则表清空，断言标志变成 `False`（Redactor 层与 Agent 层各一条）。
  这样才能把「写死」与「推导」区分开。
- 复验：把写死的 True 放回去 → `assert True is False`，精确命中。
- **通则**：写「某标志为真」的断言前，先问一句
  **「它在什么情况下应该是假？」** 答不上来，这条断言就抓不到东西。

## E-33 「写好了却从没接上」是同一个毛病的第三种形态

- 前两种是「**声明 ≠ 能力**」（E-21 / E-29）：digest 声称有能力，实际没有。
  这一种更隐蔽：**函数写得好好的、也在正确的模块里，就是没人调用它。**
- 本轮抓到两处：
  | 符号 | 该接的地方 | 后果 |
  |---|---|---|
  | `mask_account` | `log_safety.MASK_RULES` 少了它 | 字段名叫 `account` 的日志**一点没脱敏** |
  | `logout()` | 会话生命周期三件套之一 | 既没调用也没测试 |
- **排查办法**：用 `ast` 扫「顶层定义的全项目出现次数」。
  零引用的就是候选，再逐个判断是「漏接」还是「扩展点」。
- **一个必须排除的误报**：由装饰器注册的函数（如 SQLAlchemy 的
  `@event.listens_for`）看着零引用，其实注册本身就是引用。
  `_enforce_user_scope_on_write` 就属于这种 —— 别把它当死代码删了。

## E-34 规则表的**顺序**也是契约

- `MASK_RULES` 取第一个命中的规则，而 `"account_no"` 同时含 `"account"` 与 `"account_no"`。
  补账号规则时若排到银行卡规则之前，**银行卡号会被按账号掩码处理**，掩码强度下降。
- 所以除了「字段被掩码」，还补了一条「`_field_masker("account_no") is mask_bank_card`」
  的顺序守护，并真的调换顺序复验过（断言精确命中）。
- **通则**：凡是「按顺序取第一个匹配」的表（掩码规则、路由表、白名单），
  **顺序就是契约的一部分**，要单独守。

## E-35 **验收清单是「最低门槛」，不是「全部要求」**

- 计划书对本体的定义是**八要素**；而验收标准七、18 只点了其中**六项**。
- 于是测试也照着六项写：`test_ontology_has_all_six_elements_from_criterion_18`，
  外加一条给 synthesis（验收 20）。**两条合起来像齐了，实则少了 Entity。**
- 更隐蔽的是：`ENTITY` 在别处被用到（置信度同步、P5 事实），所以它「看起来是被覆盖的」——
  但它**从没对照计划书的本体要求检查过**（是否进了 PAGE_TYPES、必填字段对不对）。
- 修法：把计划书原文的完整清单显式写进测试（`PLAN_ONTOLOGY_ELEMENTS`），按它查，
  **而不是按验收清单查**；并断言数量相符，多一个少一个都红。
- **通则**：凡是「清单式」的检查，回头问一句
  **「这份清单本身穷尽了真值源吗？」**
  照着派生清单查，派生时漏掉的项会永远无人过问。

## E-36 审计也要记「查过没问题」的结论

本轮先查了两处**看起来像问题**的地方，核对后确认不是缺口：

| 疑点 | 结论 |
|---|---|
| `MAX_ROLLBACKS` 在两处各定义一份，会漂移吗 | 不会 —— 两处**各有测试钉死为 2** |
| `NO_PROGRESS_LIMIT=2` 与 `DEFAULT_NO_PROGRESS_LIMIT=3` 矛盾吗 | 不矛盾 —— 层次不同（循环轮次 vs 工具调用次数），且各自遵守项目的「（实现参数）」内联登记约定 |

- **为什么值得记**：不记的话，下一次审计还得把同样的两处再查一遍。
  审计报告只写「发现了什么」，会让人误以为其余地方没查过。

## E-37 「计数式断言」往往是循环验证

- `assert len(TABLE_13) == 18` 与 `assert len(REUSABLE_ASSETS) == 12`：
  数字来自我当初对计划书的转录，测试再拿它校验同一份转录 —— **自证不了**。
  抄漏一个，这两条照样绿。
- 本轮拿计划书原文对了一遍：表 13 = 8+4+6 = **18** ✓；资产清单 = **12** ✓，都准确。
- **但要记清楚**：这份准确性来自**仓库之外的一次人工核对**，不是仓库能自证的。
  计划书原件不在仓库里（也不该放进来），所以这类计数**只能靠人核**。
  写下来是为了免得后人误以为那两条测试提供了它们提供不了的保证。
- **通则**：看到 `assert len(x) == N`，先问那个 N 是从哪来的。
  若 N 与 x 同源，这条断言只是在防「手滑删了一行」。

## E-38 **代码与计划书有意不一致时，不一致本身就是要登记的事实**

- 计划书第 12 条资产写「**基于 Claude Code 的**全自动系统自进化闭环」，
  代码里是「全自动系统自进化闭环」—— 少了「基于 Claude Code」。
- 这不是抄漏：**裁定 U08**（人工授权）明令「不引入 Claude Code 本体」。改得对。
- 但代码里**一个字都没说明**（该条 `note` 是空的）。后果是：
  任何人都可能「照着计划书修正回去」，而那会与 U08 冲突，
  **且改回去不会有任何测试变红** —— 资产名称此前没有任何约束。
- 已补：备注写明依据 + 一条把「名称 ↔ 裁定」绑死的测试
  （并断言 U08 的内容本身没被改，依据失效时这条也会红）。
- **通则**：只写「照裁定改的」不够，要写**是哪条裁定**，并让测试绑住。
  否则后人看到差异，第一反应就是修正。

## E-39 **汇总表格会倾向于好看，不会主动说「我这里少了一条」**

- 第 13 章的 P0-2 对照表列了 8 行、每行都有交付物与验证方式，读起来像九步全覆盖。
  但计划书第一步是「Git 分支规范、CI 自动构建、**自动部署流水线**」，
  表里那行写的是「Git 分支规范 + CI」—— **把「自动部署」从原句里省掉了**。
- 缺口其实早在第 38.4 节记过，**但给人看的对照表没跟着改** ——
  缺口写在一处、汇总表里看不出来，等于没写。
- **通则**：汇总类的产物（对照表、覆盖率报告、验收清单）要拿**真值源**去对，
  不要拿它自己的措辞去对。省略比写「未落地」更危险 ——
  读者会把「没提」读成「做了」。

## E-40 守护写了两个 bug，都是靠「造出违规看它红」发现的

给对照表加守护（`tests/test_doc_coverage.py`）后照例造违规验证，连踩两次：

| bug | 现象 | 修法 |
|---|---|---|
| 查的是整节正文而非表格行 | 表前一句说明里提到了「自动部署流水线」，删掉表格行后关键词仍命中，**守护放行** | 只查表格行 |
| 表格解析没处理行间空行 | 本文档表格**每行之间夹着空行**，解析只捞到 `|---|---|---|`，而它**碰巧三列**，于是「每行都要有交付物与验证方式」**空转通过** | 逐行跳空行、遇非表格行即止 |

- 两个 bug 的共同点：**守护在真正的违规面前是绿的**，而且绿得毫无破绽。
- 这已是本项目**第五次**遇到同类问题（E-24 / E-27 / E-30 / E-32 / 本条）。
  每次都印证同一句话：**不造一次违规，你并不知道守护在不在工作。**

## E-41 **文档里的「待办」也是断言**

- 第 4 章长期写着「### 4.2 待纳入（8 项，**P3 路由落地时加入**）」，而 P3 早已落地：
  那 8 个参数都在 `TABLE_13` 里、落点模块（`scheduler.graph` / `scheduler.router` /
  `core.db` / `rag.collector`）全部可导入、`verify_table_13()` 零不一致。
- 前几轮一直在防「**声称做了其实没做**」；这次是反方向 ——
  **说「待办」而代码早已做完，一样有害**：读者会以为还有活没干，或据此重新「纳入」一遍。
- 这类错误**不会自己暴露**：没有任何测试会因为一句过期的「待办」而变红。
- 已补三条守护，其中一条守的是**前提**（那 8 项确实已落地、P3 确实已落地）——
  前提变了它会先红，提醒去改代码或改文档，而不是让守护悄悄失效。
- **通则**：文档里的「待办 / 尚未 / 计划中」都要能被机检，
  否则它们会一直躺着，越躺越不可信。

## E-42 守护会抓住「更正说明里的历史措辞」

- 我给 §4 写更正说明时，为了说清楚，把原措辞「待纳入」原样引了出来 ——
  **结果被自己刚加的守护拦下**。
- 这不是误报：守护的判据是「§4 当前不得出现该措辞」，写在说明里也算。
  改写成「尚未纳入」即可 —— 说明历史不必复现原词。
- **提示**：写「禁用词」类守护时，要意识到**连解释这条禁令的文字也会被它命中**。

## E-43 占位符漏替换，158 处字面波浪号进了正式文档



- 本项目写文档时用「波浪号包一个大写字母」表示行内代码、用类似的围栏标记表示代码块，

  目的是避免与生成脚本的模板字符串冲突。一次替换漏了，于是**字面的占位符写进了文件**：

  `docs/统一架构规范文档.md` 144 处、`MEMORY.md` 14 处。

- **为什么长期没被发现**：文档存在、内容非空、引用有效 —— 断言全过，

  只是**排版坏了**。这类错误不会让任何测试变红。

- 发现方式很偶然：改 `§28.5` 时字符串怎么都匹配不上，把原文 `repr` 出来才看见。

  **顺带解释了此前多次「锚点未命中」** —— 我按反引号写锚点，文件里却是占位符。

- 已全部替换为反引号，并在 `tests/test_docs_whitelist.py` 加了守护。



## E-44 文档与真值源矛盾，比文档过期更糟



- 第 22.1 节写「端侧语音推理适配器**尚未接入**」、第 28.5 节写「离线唤醒适配器尚未接入客户端」。

- 但适配器已全部落地，且**阶段真值源的 `note` 写的是**「从引擎到随包能力**已全部就位**」——

  **文档与自己项目的真值源直接矛盾**。两处均已按真值源改写。

- **通则**：文档要拿去和**真值源**对（代码常量、阶段真值源的 note、计划书原文），

  而不是拿它自己去对。那些「不会让测试变红」的错，只能靠主动对账发现。



## E-45 「禁用词」守护会命中解释这条禁令的文字 —— 第四次了



- 为说明占位符泄漏问题，我在文档里把字面占位符原样引了出来 —— **被自己刚加的守护拦下**。

- 这已是同类情形第四次出现（E-42 一次、本轮两次）。

- **做法**：解释时**不要把被禁的模式原样写出来**，改用文字描述（「波浪号包一个大写字母」）。

- **提示**：写「模式黑名单」类守护时，先想好**怎么在不触发它的前提下解释它**。



## E-46 内容实现了，不等于查得到对应关系



- 计划书正文嵌了 **13 张表**，每张都是硬要求。此前的可追溯性参差不齐：

  表 13 有完整的机器可核查登记、表 10/11/12 在代码里有编号引用、表 1 在第 13 章出现一行，

  而 **表 2 - 表 9 一处编号引用都没有**。

- 内容**确实实现了**（对应阶段都落地且有测试），但「计划书这张表」与「我们的交付物」之间

  **没有可查的对应关系**。审计的人只能靠通读去猜。

- 这与 E-39（对照表省掉一条）是同一类：**汇总与真值源之间缺一层索引**。

- 已补第 46 章「计划书表格索引（表 1 - 表 13）」，并加守护：

  13 张表一张都不能少，且索引指向的章节必须真实存在。

- **通则**：凡是真值源里有 N 个并列条目（表、清单、章节），

  就要有一处**显式的 N 条索引**，并让「少一条」变红。

  逐条实现是不够的 —— 还要能**一眼查到**哪条落在哪。



## E-47 计划书抽取文本的表格边界会互相渗透



- 计划书原件是 docx，抽成纯文本后**表格与其后的小节会粘连**：

  例如表 8 的抽取结果末尾粘着 P7 的小节标题，表 4 的末尾粘着 P2 的标题。

- **影响**：拿抽取文本做「表内容 ↔ 实现」的核对时，

  容易把**下一节的要求**算到当前表头上（或反过来漏掉）。

- **做法**：归纳表格主题时**按各表自身内容**，不含渗透进来的部分；

  并在文档里把这一点写明，免得后人据此误判。



## E-48 审计时别用「自己以为的名字」去找



- 核对计划书表 6 的内容时，我按字面去找 `iterations` / `no_progress` 与含 RATE/LIMIT 的常量，

  **两个都找不到，差点记成缺口** —— 实际是 `loop_count` / `rollback_count` / `tool_call_history`，

  以及 `throttle.py` 里四条 `DEFAULT_*_PER_*`。**是我猜错了名字。**

- **做法**：找不到时先看该模块**实际导出了什么**（`dir()` / 读文件），再下结论。

  否则会把已实现的功能误报成缺失，浪费一轮甚至改错东西。



## E-49 **默认值也是契约**



- `scheduler/throttle.py` 的四条默认配额（60000 / 30 / 120 / 50）测试很充分 ——

  但**全部是显式传 policy 的用例**，默认值本身没有任何断言：把 60000 改成 6 也不会红。

- 另有「单任务工具调用上限」在代码里**两个常量各定义一份**：

  `tools.DEFAULT_MAX_CALLS`（单次调度）与 `throttle.DEFAULT_TOOL_CALLS_PER_TASK`（按账号预算）。

  数值同为 50 是有意的，但**没有任何测试把它们绑在一起**。

- 已补两条守护：默认值逐个钉死；两个上限必须同步。改一个复验，两条同时红。

- **通则**：一堆「显式传参」的测试看着充分，却可能对默认值零覆盖。

  **默认值能被悄悄改掉，就等于它不存在。**



## E-50 四环境隔离：声明对了，但**文件**没人校验



- 原有四环境测试看着很全（声明互斥、文件存在、密钥不同、端口暴露策略），

  **唯独没校验四个 `.env.*` 文件里的端口 / 库 / 存储根**。

- 把 `.env.prod` 的端口与库改成 dev 的 → **没有任何测试变红**，

  而「四环境完全独立隔离」是计划书硬要求（三、P0-1）。

- 已补两条守护（读**文件**、不读声明）：值与声明一致；四文件之间五字段互不相同。

  改 `.env.prod` 复验，两条同时红。

- **通则**：「配置正确」不能靠声明对象证明，要读**真正被加载的那份**。

  `assert_disjoint()` 校验声明，`.env.*` 才决定连哪个库 —— 两者都要守。



## E-51 环境文件在**导入时**定死，运行时切不了



- `Settings.model_config` 里的 `env_file=os.environ.get("AIBUTLER_ENV", ".env.dev")`，

  **写在类体里，只在导入时求值一次**。

- 所以进程内改 `AIBUTLER_ENV` 无效；必须**在启动前**设好。

  compose 与 pytest 夹具（conftest 在导入前设）都是这么用的，生产用法没问题。

- 但 `load_settings(**overrides)` 是公开 API，不传 `_env_file=`，

  **照直觉写「运行时切环境」会静默拿到 dev 配置** —— 这正是危险之处（可能连错库）。

- 排查时一度以为是 `lru_cache` 的锅，清了缓存仍然如此，才定位到类体求值。

  **提示**：怀疑缓存前，先看那个表达式在**哪一层**求值。



## E-52 模板变量缺失不会当场报错，只会静默变成空值



- compose 对未定义变量的处理是**静默替换成空串**：`${API_PORT}:8000` 会变成 `:8000`。

- 本项目 `deploy/compose/local-ports.yml` 引用了三个变量，

  由四个 `.env.<环境>` 提供 —— **但此前没有测试校验「供应方是否都提供了」**。

- 已补守护：从模板解析出全部 `${VAR}`，要求每个 env 文件逐个提供。

- 另补：`API_PORT` 此前漏在环境文件守护之外（只查了 PG/Redis/库/存储根），现已纳入。

- **通则**：凡是用 `${VAR}` 的模板（compose、CI、Dockerfile），

  都要有一条「模板引用的变量，供应方是否都提供了」的守护 ——

  **延迟绑定意味着「缺一个」不会当场报错。**



## E-53 守护红了还不够，要说清楚**病灶在哪**

- 从 `.env.prod` 删掉 `API_PORT` 复验时，两条守护报的是裸 `KeyError`，

  **不说是哪个文件缺的** —— 四个环境里排查要多跑几轮。

- 加了 `_need(data, key, where)` 后，报错变成

  「`.env.prod 缺少必需变量 API_PORT`」，一眼定位。

- 另：写这类「取必需键」的辅助函数时注意 —— `data[key]` 在四环境里普遍使用，

  一次替换到位即可，别只改一半（否则有的地方报得好、有的地方还是裸 KeyError）。



## E-54 「有意不做」也必须写下来，否则与遗漏无从区分



- CI 没跑三项（`pytest -m live`、`verify_android_artifact.py`、Flutter 外壳的 analyze），

  **三项都有正当理由**（要真实 Ollama / 本地 BGE 模型 / 构建产物 / Flutter SDK）。

- 但 **CI 与文档里都没说**，读者会以为 CI 覆盖了全部 —— 与遗漏无从区分。

- 更实际的问题是：`REQUIRED_COMMANDS` 是手工清单，

  **新加一个 `scripts/verify_*.py` 可以悄悄地既不进 CI、也不进任何名单**。

- 已补三条守护：扫**磁盘上的脚本**逼着分类；排除名单里的脚本必须存在；

  「有意跳过 live 标记」写成断言。

- **通则**：前几轮防「声称做了其实没做」，这一轮防

  **「没做，且没说是故意的」**。两者都要能被机检。



## E-55 会误报的守护，很快就会被无视

- 「只允许 live 一种选择性标记」那条守护第一版把所有 `@pytest.mark.*` 都算进来，

  于是抓到了 `parametrize` —— 而它是**结构性标记**，不参与 `-m` 选择。

- 已列出结构性集合（parametrize / skipif / xfail / filterwarnings / usefixtures），只盯会造成排除的。

- **误报也要当缺陷修**：一个会喊狼来了的守护，很快就会被无视，

  那时它连真问题也拦不住了。



## E-56 扫描器报出来的东西，要逐个回原文核



- 自写脚本扫「未声明的第三方 import」，报出三个：`jwt` / `frontmatter` / `sentence_transformers`。

- **三个全是误报**：前两个是 `PyJWT` / `python-frontmatter` 的导入名与 PyPI 名不同；

  第三个声明在 `requirements-ml.txt`（重型 ML 依赖单独一份），且代码里是延迟导入。

- 与 E-48 同型：**判据不等于事实**。扫描器给的是候选，不是结论。

- 项目本来就有 `tests/test_dependency_declaration.py`（5 项）在守这件事 —— 

  写扫描器前先搜一下有没有现成的守护，能省一轮。



## E-57 「查过没问题」要连判据一起记



- 审计只写「发现了什么」，读者会以为其余地方没查过，下一轮还得重查。

- 本轮结论：依赖声明这块**本来就是对的，无需改动**；

  连同「我误报了什么、项目已有的守护叫什么」一并记录。

- 与 E-36 是同一原则的第二次应用：**负面结果也是结果**。



## E-58 风控不是「加一层」，而是「加一道闸门」



- 用户要求「做好风控（不能被用户以虚假信息或 base 代码混淆）」，

  但五层路由是**冻结**的 —— 加第六层等于违反【八】。

- 解法：做**前置闸门** `core/input_guard.py`，三档 ALLOW / TAINT / BLOCK，

  接在 `IntentRouter.route()` 最前面，不进 LAYERS。并发一条测试守住这一点。

- **预期中的坑**：中文常把动词放后面（「系统提示词**打印**出来」），

  而注入模式按「打印+系统提示词」的顺序写就漏了 —— 补了反向语序。

- **判真假不是程序的事**：虚假信息一律 TAINT（只降可信度），

  有可核验证据位（URL/DOI/文件）则免除；真正能否入库交下游证据链决定。



## E-59 环境休眠后第一步是查依赖服务，不是查代码

- 隔了近两天再跑，全量测试突然大量 `ConnectionRefusedError`。

  一度怀疑是我改的代码有问题 —— 实际是 **Docker Desktop 没在运行**，PG/Redis 全没了。

- 处置：找到 Docker Desktop 真实路径（不在 `C:\Program Files`，

  而在 `%LOCALAPPDATA%\Programs\DockerDesktop`）→ 启动 →

  `docker compose -f base.yml -f local-ports.yml --env-file .env.test up -d`。

- `api` 镜像因 registry 不可达没构建成，但**跑测试只需要 PG + Redis**。

- **教训**：连接被拒先看服务，别先怀疑自己刚改的代码。



## E-60 接上主链路，还要接上**失败路径**



- 风控闸门接进了 `IntentRouter.route()`，测试全绿 —— 但它会抛 `InputGuardError`，

  而 `graph.py::_node_route` 直接 `await self.router.route(...)`，**没有 try/except**。

  结果是：恶意输入能把整个 Graph 抛崩，而不是礼貌拒绝。

- 与计划书口径不一致：会话失效那条是**返回状态、终止流程**，不是抛异常。

- 修法：捕获后返回**不带 intent** 的状态，让 `_branch` 自然走到 `NODE_TERMINATE`。

  **不新增节点、不改边** —— 借用图本来就有的「无 intent 即终止」分支。

- **通则**：接新能力时除了问「调用点对不对」，还要问「**它失败时会发生什么**」。

  失败路径没接好，等于给主链路埋雷，而这个雷只有真出恶意输入时才炸。



## E-61 **算出结论 ≠ 约束生效**



- 风控判出 TAINT 后，说明写进了 `RouteDecision.degradation_notes`；

  而 `graph.py::_node_route` **只在 require_clarification 分支**回写它 ——

  正常流程下结论被整条丢弃，没有任何下游知道输入已不可信。

- 「不得沉淀为事实」于是成了一句没人执行的话。

- 修法：加**显式字段** `input_untrusted` 贯通 RouteDecision → GraphState → SchedulerResult，并列入 PROTECTED_STATE_FIELDS。

- 实测中先拦下「子 Agent 试图洗白」的是**写作用域校验**（P4 仅写自有字段），PROTECTED_STATE_FIELDS 是第二层。

- **通则**：A 判定、B 执行的约束，必须有测试证明 **B 真的收到了 A 的结论**；只测 A 算得对，等于没测。



## E-62 判定 → 传递 → **无人执行**：约束还是没生效



- 前一节把 TAINT 结论贯通到 GraphState / SchedulerResult 后，我写了「下游据此拒绝把输入沉淀为事实」。

- 本轮 grep 发现 `input_untrusted` 只有**定义、传递、保护、测试**，**零消费者** ——

  缺口只是从「没传到」变成了「传到了没人看」。

- 收口点是 `FactWriter`（子 Agent 产出事实的唯一出口）：

  `untrusted=True` 时 Claim 落成 `SUSPECTED` 而非 `TRUE`。

- **为什么不是拒绝写入**：页面留着才能人工复核，丢弃等于把线索也扔了。

- **通则**：写「下游会据此…」这句话之前，先 grep 一下下游**真的**在哪。

  这已是同类问题第三次（E-60 失败路径、E-61 结论去向、本条执行点）。



## E-63 「写公共库还是私有库」只差一个参数，而没人守



- `WikiStore.write_page(page, user_id=...)`：传了 user_id 写私有 Vault，

  不传就写公共 workspace —— **全员可见**。

- 查下来当前没有用户可触发的公共写入路径（materials 是系统归档；

  p6 那条我一开始 grep 只截到首行，误以为是公共写，实际传了 user_id）。

- 但**这个保证没有机制守**：以后新加一处漏传 user_id 的写入，没有任何测试会红，

  后果是全体用户看到虚假信息。

- 修法与 E-54（CI 脚本归属）同型：AST 扫出所有没传 user_id 的 write_page，

  要求在白名单里登记理由；反过来也检查白名单是否已失效。

- **通则**：越是「一个参数决定安全边界」的 API，越需要白名单式守护。

  靠 code review 记性是不够的。



## E-64 文档里的数字也是断言



- 收尾核计数时抓到：`统一架构规范文档` 写「全场景用例库：397 条」，实际 **463 条**。

  用例库一路上涨，文档里的数字一直没跟 —— **没有任何测试会因此变红**。

- 与 E-41 同型（文档说「待办」而代码早已做完）：**文档里的数字也是断言**。

- 已加守护：从文档抽出「全场景用例库：N 条」的 N，与真实条数比对；

  把文档改回 397 复验，精确报「文档写 397 条，实际 463 条」。

- **提示**：这类守护的判据是正则，改文案容易让它失配 ——

  所以守护里要**先断言「确实抽到了数字」**，否则文案一改它就会静默空转。



## E-65 分支规范与 CI 都齐了，唯独**仓库本身从来不存在**

- 接管时发现：架构规范第 12 章把分支模型（main / develop / feature/* / release/* / hotfix/*）

  写得清清楚楚，`.gitea/workflows/ci.yml` 也早就就位，`.gitignore` 甚至把 `.env.*`

  和 `models/` 都排干净了 —— 但项目根下**没有 `.git`**，从来没 `git init` 过。

- 这是「写好了却从没接上」的第四种形态（E-21 / E-29 / E-33 之后又一次）。

  前三形态是「声明了能力」「代码没接主链路」「写了没接上」；

  这次更彻底：**承载这一切的容器都不存在**，CI 没有任何可运行的前提。

- 更讽刺的是规范第 13 章那一行写着「Git 分支规范 | 第 12 章 | 分支表与 CI 触发条件一致

  （`tests/test_ci_pipeline.py`）」—— 而那个文件**根本没有这条断言**。

  **文档引用了一个不存在的守护。**（与 E-36「查过没问题要连判据一起记」同源）

- 补法：`git init -b main` + 建 `develop`，入库 247 个文件；新增 `tests/test_repo_hygiene.py`

  守四条。关键一条是**用 `git check-ignore` 反向验 `.gitignore` 规则本身**——

  只看「该文件当前是否被跟踪」在文件不存在时恒绿，抓不到规则缺失（E-27）。

- 已按 E-40 造违规复验：注释掉 `.env.dev` 规则、删掉 ci.yml 的 `hotfix/**`，两个守护都变红。

- **另有一条待办**：`git init` 前先查了大文件——`mobile/build` 下有约 2 GB 构建产物，

  靠 `mobile/.gitignore` 的 `/build/` 排除；根 `.gitignore` 又补了一层兜底，

  防的是「将来 mobile 挪位置后规则失效」这类静默退化。

- **教训**：验收一个能力有没有落地，要先问「**它的载体在不在**」。

  规范、配置、守护三层都齐了，仍可能整件事从未真正跑起来过。


---

## 二、环境基线（复现上述解决方案所必需）

- 操作系统：Windows AMD64，16 逻辑核，内存 11 GB，C 盘可用 36.5 GB / D 盘可用 106.7 GB
- GPU：NVIDIA RTX 4060 Laptop 8 GB（Python 侧 torch 为 CPU 版，GPU 仅 Ollama 可用）
- 运行时：Python 3.12.7、Node v24.19.0、Git 2.55.0、Docker 29.8.0
- 本地模型：Ollama `qwen2.5:7b-instruct-q4_K_M`（capabilities: tools，实测 38.6 tok/s）
- 项目解释器：`ai-butler-ecosystem\.venv`（`--system-site-packages`，继承 langgraph / chromadb / langchain / fastapi 等）

---

## 三、Jev 失败 case 与 Wiki 矛盾案例

（P0-1 尚未产生；P3 / P6 起按计划书要求分类沉淀。）
