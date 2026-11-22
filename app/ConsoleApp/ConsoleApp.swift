// ModelTower：每台 Mac 跑一个 oMLX，单机加载一个模型。
//
// 应用只负责显示和点按钮。所有操作都调用 scripts/clusterctl.py 的命令行子命令，
// 逻辑只维护那一份；应用自己不读 settings.json，也不接触任何密钥明文。
// 设备版和 oMLX 同进退（用户 11-04 定）：打开就启动 oMLX，不加载模型；红色关闭按钮只收起窗口；退出才一起关。不开机自启，设置里能开。
// 有哪些机器、哪些模型都听 status --json 的（来自 cluster.json 和型号档案），这里不写死。

import AppKit
import CryptoKit
import LocalAuthentication
import ServiceManagement
import SwiftUI
import UniformTypeIdentifiers
import WebKit

// MARK: - 和 clusterctl.py 的约定（status --json）

struct Status: Decodable {
    let ok: Bool
    let time: String?
    let mode_text: String?
    let busy: Bool?
    var nodes: [String: NodeInfo]?
    let models: [String: ModelMeta]?
    let cache_info: [[String]]?       // 多轮缓存 ⓘ 的 [标题, 正文]
    let cache_recommended: Int?
    let advice: String?
    let ollama: [OllamaModel]?
    let keys: Keys?
    let pages: [String: String]?   // 机器名 → 管理页；chat → 本机的对话页
    let order: [String]?
    let links: [[String]]?           // 改地址时连接方式的选项：[存的值, 显示的字]
    let model_order: [String]?
    let model_recency: [String]?     // 默认顺序：新模型靠前
    let tools: Tools?                // 这台装了哪些个人工具（2b）；旧版 clusterctl 没有，就当都装着、照旧显示
    let engine: EngineInfo?          // 第 3 期：只有官方版、没装 oMLX 的机器才有；自管版（你两台）没有这一项，界面和以前一样
    let setup: SetupInfo?            // 第 4 期："开始使用"三步，同样只有官方版、没装的机器才有
    let error: String?
}

/// "开始使用"的一步（草案 11.2）。后端只给代号和数字，文字在这边按语言写；字段都可能缺
struct SetupStep: Decodable {
    var id = ""               // engine model load
    var done = false
    var state: String?        // engine：missing stopped running
    var count: Int?           // model：这台有几个模型
    var need_key: Bool?       // model：还没允许读主密钥，看不到有哪些模型
    var memory_gb: Int?
    var suggest_gb: Int?      // 建议挑多大以内的模型
    var china: Bool?          // 下载源选了"中国"：提示在 oMLX 的下载页选魔搭
    var first: String?        // load：先加载哪个

    private enum Field: String, CodingKey { case id, done, state, count, need_key, memory_gb, suggest_gb, china, first }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: Field.self) else { return }
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        done = (try? c.decode(Bool.self, forKey: .done)) ?? false
        state = try? c.decode(String.self, forKey: .state)
        count = try? c.decode(Int.self, forKey: .count)
        need_key = try? c.decode(Bool.self, forKey: .need_key)
        memory_gb = try? c.decode(Int.self, forKey: .memory_gb)
        suggest_gb = try? c.decode(Int.self, forKey: .suggest_gb)
        china = try? c.decode(Bool.self, forKey: .china)
        first = try? c.decode(String.self, forKey: .first)
    }
}

struct SetupInfo: Decodable {
    var steps: [SetupStep] = []
    var done = false
    var next: String?         // 轮到哪一步；都做完是 nil

    private enum Field: String, CodingKey { case steps, done, next }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: Field.self) else { return }
        steps = (try? c.decode([SetupStep].self, forKey: .steps)) ?? []
        done = (try? c.decode(Bool.self, forKey: .done)) ?? false
        next = try? c.decode(String.self, forKey: .next)
    }
}

/// 个人工具检测到才出现（2b，草案 9.5）：dsh 看配置文件，Ollama 看端口答不答，H3 看脚本和环境
struct Tools: Decodable { let dsh: Bool?; let ollama: Bool?; let h3: Bool? }

/// 这台的 oMLX 是哪种、版本落在兼容表哪一档、允不允许控制台读主密钥（第 3 期，草案 10.3）。
/// 每个字段单独读，哪个读不懂就当没有：这里出错不能把整个状态读坏
struct EngineInfo: Decodable {
    var kind = ""             // official：官方版；none：没装
    var kind_text: String?
    var how_text: String?     // oMLX 应用、Homebrew、别的装法
    var port: Int?            // oMLX 自己的端口
    var client_port: Int?     // 客户端该连的端口（有"入口"时是入口的）；clusterctl 没给就用 port
    var version: String?
    var verdict: String?      // verified works testing bad old newer unlisted unknown no_table
    var verdict_text: String?
    var note: String?
    var key: String?          // allowed denied ask
    var gateway: String?      // 入口：running stopped off elsewhere（已定第 29 条 ②：官方版没表过态就算开）
    var gateway_set: String?  // default on off：用户自己开关过没有
    var gateway_note: String? // 起不来的原因（端口被谁占着）
    var client_urls: [ClientURL]?   // 客户端能填的地址（0.5.1 只有本机一行；0.6 加局域网）

    private enum Field: String, CodingKey { case kind, kind_text, how_text, port, client_port, version, verdict, verdict_text, note, key, gateway, gateway_set, gateway_note, client_urls }

    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: Field.self) else { return }
        kind = (try? c.decode(String.self, forKey: .kind)) ?? ""
        kind_text = try? c.decode(String.self, forKey: .kind_text)
        how_text = try? c.decode(String.self, forKey: .how_text)
        port = try? c.decode(Int.self, forKey: .port)
        client_port = try? c.decode(Int.self, forKey: .client_port)
        client_urls = try? c.decode([ClientURL].self, forKey: .client_urls)
        version = try? c.decode(String.self, forKey: .version)
        verdict = try? c.decode(String.self, forKey: .verdict)
        verdict_text = try? c.decode(String.self, forKey: .verdict_text)
        note = try? c.decode(String.self, forKey: .note)
        key = try? c.decode(String.self, forKey: .key)
        gateway = try? c.decode(String.self, forKey: .gateway)
        gateway_set = try? c.decode(String.self, forKey: .gateway_set)
        gateway_note = try? c.decode(String.self, forKey: .gateway_note)
    }

    var official: Bool { kind == "official" }
    /// 官方版、还没允许读主密钥：看不到模型，加载、卸载、子密钥、日志、多轮缓存都用不了
    var keyBlocked: Bool { official && key != "allowed" }
    /// 主界面要提醒的版本：太旧、有已知问题、读不到版本、没有兼容表。比已验证的新、表里没列的只在设置里写，不打扰
    var warnsVersion: Bool { official && ["old", "bad", "unknown", "no_table"].contains(verdict ?? "") }
}

/// 一台机器。checked 为 false 时（快速刷新不走 SSH）没有内存这些字段
struct NodeInfo: Decodable {
    let title: String
    let role: String
    let kind: String?    // hub、worker。role 只管显示，翻译后不能拿它做判断；旧版本的控制台没有这个字段
    var isHub: Bool { kind == "hub" || (kind == nil && role == L("主机")) }
    let local: Bool?              // 这台就是运行控制台的本机；10-30 以前的 clusterctl 没有这个字段
    let reachable: Bool
    let checked: Bool
    let service: String           // up / starting / reconnecting / down
    let url: String
    let models: [String: String]  // 模型名 → loaded / loading / off
    let stats: [String: ModelStat]
    let omlx_gb: Int?
    let contexts: [String: NodeContext]?   // 模型名 → 这台的上下文
    let options: [String: [String: OptionState]]?   // 模型名 → "mtp" / "ssd" → 这台的开关
    let dsh: [String: Bool]?                // 模型名 → dsh 里有没有「这台 · 这个模型」；没装 dsh 为 nil
    var fits: [String: Bool]?               // 没在跑的模型 → 不卸载别的、一起放放不放得下；这台空着或读不到内存时没有
    let lock: LockInfo?                     // 谁正占着这台的操作锁；没人占没有这个字段
    let code_ok: Bool?                      // 走 rpc 的机器：它的控制台程序和这台是不是同一版
    let transport: String?                  // "rpc"：配对加入、走受限钥匙的副机；本机和走全权限 SSH 的老路没有这个字段
    var isRpc: Bool { transport == "rpc" }
    var time: String?
    var used_gb: Int?
    var total_gb: Int?
    var swap: String?
    var pressure: Int?           // 内核的内存压力：1 正常、2 警告、4 严重
    var line_gb: Int?            // 安全线（总内存的 percent%）
    var gpu: Int?
    var disk_free_gb: Int?
    var cache_gb: Double?        // 多轮缓存已用
    var cache_limit_gb: Int?     // 上限：0 是关，读不到（比如 auto）没有这个字段
    var cache_auto: Bool?        // 官方 oMLX 的上限是它默认的"自动"（这时没有 cache_limit_gb）；自管版没有这个字段
    var cache_auto_ok: Bool?     // 能选回"自动"（官方版，0.5.1 A14）；自管版没有这个字段
    var cache_options: [Int]?    // 能选的上限；关总能选
    var need_gb: [String: Int]?  // 每个模型在这台加载要多少内存
    var waiting_unlock: Bool?    // 连不上但 22 端口通：开着，多半在等 FileVault 解锁
    var power: String?           // 主机关过它之后：going_down / stuck / off
    var power_since: Double?
    var power_fresh: Bool?       // 刚断开（约 60 秒内，D4）；11-10 起界面不再分开说（已定第 26 条）
    var dsh_title: String?       // dsh 配置里的显示名，改名后不跟着变
    var alert: [String]?         // 超线原因（红）；空表示正常
    var warn: [String]?          // 还没超、但对话塞满上下文后会超（黄）
    var omlx_now_gb: Int?        // oMLX 进程此刻的实际占用（内核记录）
    var max_gb: Int?             // 对话塞满上下文时整台最多约多少
    var leftover_procs: Int?
    var desktop: Bool?           // 远程那台登录了桌面没有（完整刷新才有）；停在登录界面时是 false（T1）
    let wants_up: Bool?          // 关机前 oMLX 在跑：回来时主机自动启动它（D1）
    let addresses: [AddressRow]? // 登记的地址，网线在前
    let address: String?         // 现在连的是哪个地址
    let address_note: String?    // 连不上、.local 解析出机器表里没有的地址：地址可能变了
    let has: [String]?           // 这台有哪些模型（2b 每台一份清单）；旧版 clusterctl 没有，就照旧列全部
    let meta: [String: ModelMeta]?   // 这台的说法和主机的不一样时才有（你的编辑存在模型所在的那台）

    var up: Bool { service == "up" }
    var active: [String] { modelOrder.filter { (models[$0] ?? "off") != "off" } }
    var loaded: String? { modelOrder.first { models[$0] == "loaded" } }
    var loading: String? { modelOrder.first { models[$0] == "loading" } }

    /// 快速刷新只更新服务和模型，内存等沿用上一次完整刷新
    func keepingMachine(from old: NodeInfo) -> NodeInfo {
        var n = self
        n.time = old.time; n.used_gb = old.used_gb; n.total_gb = old.total_gb; n.swap = old.swap
        n.pressure = old.pressure; n.line_gb = old.line_gb; n.gpu = old.gpu; n.disk_free_gb = old.disk_free_gb
        n.cache_gb = old.cache_gb; n.cache_limit_gb = old.cache_limit_gb; n.cache_auto = old.cache_auto; n.cache_auto_ok = old.cache_auto_ok; n.cache_options = old.cache_options; n.alert = old.alert; n.warn = old.warn; n.omlx_now_gb = old.omlx_now_gb
        n.max_gb = old.max_gb; n.leftover_procs = old.leftover_procs; n.fits = n.fits ?? old.fits; n.desktop = old.desktop
        return n
    }
}

struct LockInfo: Decodable { let who: String?; let what: String?; let since: String? }

/// 一台的一个地址（机器表的 addresses），machine addresses set 收的就是这样一组
struct AddressRow: Codable, Hashable { var ip: String; var interface: String; var link: String }
/// machine ports：这台有哪些网口、现在各是什么地址
struct PortRow: Decodable, Hashable { let interface: String; let port: String; let link: String; let ip: String }
struct PortList: Decodable { let ports: [PortRow] }

/// machine list --json：这台能控制哪些机器；pending 是已生成配对串、对方还没允许的
struct MachineList: Decodable { let machines: [MachineRow]; let pending: [PendingMachine] }
struct MachineRow: Decodable, Identifiable { let name: String; let title: String; let role: String; let local: Bool; let transport: String; var id: String { name } }
struct PendingMachine: Decodable, Identifiable { let name: String; let title: String; let ip: String; var id: String { name } }
/// machine add --json：给对方用的配对串、核对码，和不装控制台时手工贴的那一行
struct PairInfo: Decodable { let ok: Bool; let name: String; let code: String; let pair: String; let line: String; let error: String? }
/// machine check --json：配好后自检六项（第 4 期）。state：ok 通过、fix 要处理、note 只是提醒、unknown 查不了
struct MachineCheck: Decodable {
    struct Item: Decodable, Identifiable { let id: String; let title: String; let state: String; let hint: String }
    let name: String
    let title: String
    let items: [Item]
    let fix: Int
}
/// grants --json：这台授权给了谁；others 是不是控制台加的行（全权限，比如维护通道）
struct GrantList: Decodable { let grants: [GrantRow]; let others: Int }
struct GrantRow: Decodable, Identifiable { let id: String; let level: String; let title: String }

/// clusterctl requests 的结果：那台此刻在算、排队的请求数
struct RequestCounts: Decodable {
    let active: Int
    let waiting: Int
}

struct ModelStat: Decodable {
    let requests_active: Int?
    let requests_waiting: Int?
    let speed_tps: Double?
    let speed_live: Bool?         // true：正在生成的实时速度；false：本次服务启动以来的平均
}

struct ModelMeta: Decodable {
    let id: String
    let title: String
    let note: String
    let eta: String
    let vision: Bool
    let size_gb: Int
    let info: [String: [[String]]]?   // "mtp" / "ssd" / "context" → ⓘ 的 [标题, 正文]，文案在 clusterctl 里维护
    let version: String?              // 卡片头的版本号，如 V4.1 Flash
    let label: String?                // 卡片头的短标签：满血版、满血无损版、27B Q4；识别在 clusterctl
    let about: [[String]]?            // 卡片头 ⓘ：精度、能做什么、推荐场景
    let warmup: Bool?                 // 开 MTP 后第一次加载要预热（DeepSeek）；旧版 clusterctl 没有
    let estimated: Bool?              // 自动识别的模型：内存是估算的
}

/// 一台上一个模型的一个开关（10-31）。on 是存的值；live 是正在跑的值（没在跑为 nil）；pending：改了、下次加载才生效。
/// mode：free 可开关｜locked 锁定为开（DeepSeek 的 Engram 表）｜na 不适用（MiniMax 没有查询表）｜
/// unsupported 官方 oMLX 说这个模型不支持（第 3 期，显示但灰掉），reason 是 oMLX 给的原文，可能没有
struct OptionState: Decodable {
    let on: Bool
    let mode: String
    let live: Bool?
    let pending: Bool
    let reason: String?
}

/// 一台上一个模型的上下文（10-30 起按机器分开设）。live 是 oMLX 里此刻的上限，没加载为 nil；
/// options 是模型支持的档位，加上正在用的；marks 是"已实测，测到 26.9 万 / 未实测"，notes 是"推荐 / 可选…"，两回事；
/// compact 是 dsh 什么时候压缩；over 是对话塞满时超内存安全线的档位（照样能选，选时再提醒）
struct NodeContext: Decodable {
    let setting: Int
    let live: Int?
    let native: Int
    let options: [Int]
    let marks: [String: String]
    let notes: [String: String]?
    let compact: [String: String]?
    let over: [Int]?
    let recommended: Int?
    let max_output: Int?
}

/// progress --json：卡住报警。state 为 idle / working / stalled / down / unknown
struct ProgressInfo: Decodable {
    let ok: Bool
    let state: String
    let text: String?
    let still_seconds: Int?
    let node: String?
}

struct OllamaModel: Decodable { let name: String; let size_gb: Int }
/// 客户端能填的一个地址：id 是 local（0.6 起还有 lan 等），url 带 /v1
struct ClientURL: Decodable, Identifiable { let id: String; let name: String; let url: String }
struct Keys: Decodable { let note: String; let sub_keys: [SubKey]; let can_disable: Bool? }   // can_disable：入口开着且在这台，停用才起作用
struct SubKey: Decodable, Identifiable {
    let name: String
    let created_at: String
    let hint: String
    let disabled: Bool?
    var id: String { name + hint }
}

/// hub view：现在谁是主机、这台能做什么；不含任何密钥
struct HubView: Decodable {
    let enabled: Bool
    let me: String?
    let hub: String?
    let term: Int?
    let is_hub: Bool?
    let candidates: [String]?
    let machines: [String: String]?
    let has_secret: Bool?
    let applications: [String]?
    let locked: Bool?
    let gateway: Bool?
    let password_set: Bool?
    let password_at: String?
    let password_where: String?
    let needs_resign: Bool?
}

/// chip view：登记了哪些钥匙、谁在申请。钥匙本身在各台的安全芯片里，这里只有公开的信息
struct ChipView: Decodable {
    struct Key: Decodable, Identifiable { let id: String; let title: String; let added: String; let fingerprint: String }
    struct Request: Decodable, Identifiable { let id: String; let title: String; let pub: String; let `where`: String? }
    struct Pending: Decodable { let root: String; let title: String; let seq: Int; let digest: String }
    let enabled: Bool
    let seq: Int
    let keys: [Key]
    let recovery_at: String?
    let recovery_id: String?
    let password_only: Bool
    let requests: [Request]
    let root: String?
    let pending: Pending?
    let is_hub: Bool?
    let waiting: [String]?
}

/// hub state：最高操作核对时要用的现状。管理版连着备用机接管时也要能读，所以不走 hub view（那条只有主机答）
struct TopState: Decodable { let enabled: Bool; let term: Int?; let hub: String?; let candidates: [String]?; let password_set: Bool? }

/// admin-session：管理页地址和一个登录会话；主密钥不经过这里
struct AdminSession: Decodable { let ok: Bool; let url: String; let name: String; let value: String }

/// prepare：这台 Mac 准备好了没有；command 是"一次设好"要用管理员身份跑的那一行
struct PrepareInfo: Decodable { let remote_login: Bool; let autorestart: Bool; let no_sleep: Bool; let can_poweroff: Bool; let filevault: Bool; let command: String; let host_key: String? }

/// subkey where：一把子密钥在哪几台上；明文留在各台，这边只有名字和末 4 位
struct KeyCoverage: Decodable { let order: [String]; let unreachable: [String]; let sub_keys: [CoveredKey] }
struct CoveredKey: Decodable, Identifiable {
    let name: String
    let created_at: String
    let hint: String
    var disabled: Bool? = nil
    let on: [String]
    var id: String { name + hint }
}

/// 机器顺序：每次刷新按 status 的 order 更新（clusterctl 读 cluster.json）；记在偏好里，下次打开时读到之前也能先画出卡片
var nodeOrder = UserDefaults.standard.stringArray(forKey: "nodeOrder") ?? []
/// 模型顺序：每次刷新按 status 的 model_order 更新（clusterctl 的 MODELS 是唯一来源）；这里是读到之前的默认值
var modelOrder: [String] = UserDefaults.standard.stringArray(forKey: "lastModelOrder") ?? []
/// 机器在机器表里的顺序，定颜色用：调卡片顺序时颜色不跟着变
var tintOrder: [String] = []

/// 默认顺序（用户 11-04 定）：本机最前，主机其次，其余按添加顺序
func defaultNodeOrder(_ s: Status) -> [String] {
    let rank = { (k: String) in s.nodes?[k]?.local == true ? 0 : s.nodes?[k]?.isHub == true ? 1 : 2 }
    return (s.order ?? []).enumerated().sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }.map(\.element)
}

/// 用户调过的顺序优先；新出现的，机器排在后面，模型排在前面
func customized(_ base: [String], saved: [String]?, newFirst: Bool = false) -> [String] {
    guard let saved = saved else { return base }
    let kept = saved.filter(base.contains), extra = base.filter { !saved.contains($0) }
    return newFirst ? extra + kept : kept + extra
}

/// 8192 → 8K，1048576 → 1M
func contextLabel(_ n: Int) -> String { n >= 1_048_576 ? "\(n / 1_048_576)M" : "\(n / 1024)K" }
func gbText(_ v: Double) -> String { v < 10 ? String(format: "%.1f GB", v) : "\(Int(v.rounded())) GB" }
func diskText(_ gb: Int) -> String { gb >= 1000 ? String(format: "%.1f TB", Double(gb) / 1024) : "\(gb) GB" }
func pressureText(_ p: Int?) -> String { p == 4 ? L("压力严重") : p == 2 ? L("压力警告") : L("压力正常") }

let machineTints = [Color(red: 0.11, green: 0.62, blue: 0.46), Color(red: 0.50, green: 0.47, blue: 0.87),
                    Color(red: 0.85, green: 0.45, blue: 0.20), Color(red: 0.20, green: 0.55, blue: 0.85)]
func machineTint(_ node: String) -> Color { machineTints[(tintOrder.firstIndex(of: node) ?? nodeOrder.firstIndex(of: node) ?? 0) % machineTints.count] }

// MARK: - 调用命令行

enum Backend {
    /// 公开版（第 6 期）：后端脚本在应用包里，不认测试用的环境变量，不露出没发布的功能
    static let isPublic = (Bundle.main.object(forInfoDictionaryKey: "LMCEdition") as? String) == "public"
    /// 主机模式、最高密码、安全芯片、管理版这一套的入口和说法露不露：公开版这次不发布（已定第 2 条），一律不露；内部的构建照旧。
    /// 要藏的地方都认这一个开关，别处不直接判断 isPublic
    static let showsHub = !isPublic
    /// 联机（添加机器、本机授权）露不露：公开版 0.5.1 是单机版，这两页换成预告页，1.0 正式版打开（已定第 33 条）；代码留着，内部的构建照旧
    static let showsPairing = !isPublic
    /// 应用包里自带的后端（公开版的构建会放进去）；没有就是内部的装法，路径由 build_console_app.sh 写进 Info.plist
    static let embedded: String? = {
        let path = (Bundle.main.resourcePath ?? "") + "/console"
        return FileManager.default.fileExists(atPath: path + "/scripts/run.py") ? path : nil
    }()
    static let project = embedded ?? (Bundle.main.object(forInfoDictionaryKey: "LMCProjectPath") as? String)
        ?? FileManager.default.currentDirectoryPath
    /// 自带的后端经启动器跑：系统自带的 Python 3.9 直接读 clusterctl.py 会报错，导入没事
    static var script: String { project + (embedded != nil ? "/scripts/run.py" : "/scripts/clusterctl.py") }
    /// 找到的 Python；nil 是这台上一个能用的都没有（主窗口换成说明页，装好后点"重新检查"）
    private static let pythonLock = NSLock()
    private static var foundPython: String? = findPython()
    static var pythonPath: String? {
        get { pythonLock.lock(); defer { pythonLock.unlock() }; return foundPython }
        set { pythonLock.lock(); foundPython = newValue; pythonLock.unlock() }
    }
    /// 自带后端的构建用的可能是 oMLX 应用里带版本号的那个目录，oMLX 升级后目录名会变：不在了就重新找一次。内部的装法不查，照旧
    static var python: String {
        if let path = pythonPath, embedded == nil || FileManager.default.isExecutableFile(atPath: path) { return path }
        let again = findPython()
        pythonPath = again
        return again ?? "/usr/bin/false"
    }

    static func findPython() -> String? {
        let fm = FileManager.default
        func usable(_ path: String) -> Bool { fm.isExecutableFile(atPath: path) }
        if let resources = Bundle.main.resourcePath, usable(resources + "/python/bin/python3") { return resources + "/python/bin/python3" }
        if let written = Bundle.main.object(forInfoDictionaryKey: "LMCPython") as? String, !written.isEmpty { return written }   // 内部的装法：照旧，不检查
        if embedded == nil { return "/Users/Shared/mlx-cluster/.venv/bin/python" }
        // oMLX 应用自带一个 Python（装了还没打开过也有）
        for root in ["/Applications", NSHomeDirectory() + "/Applications"] {
            let base = root + "/oMLX.app/Contents/Resources/Python"
            for name in ((try? fm.contentsOfDirectory(atPath: base)) ?? []).sorted().reversed() where usable(base + "/" + name + "/bin/python3") {
                return base + "/" + name + "/bin/python3"
            }
        }
        // /usr/bin/python3 在没装命令行工具时是个会弹安装框的替身，所以看真正的位置
        for path in ["/Library/Developer/CommandLineTools/usr/bin/python3", "/Applications/Xcode.app/Contents/Developer/usr/bin/python3",
                     "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
                     NSHomeDirectory() + "/.omlx/bin/omlx-cluster-python"] where usable(path) { return path }
        return nil
    }

    /// 管理版：没有 oMLX 底座，命令经 SSH 让主机跑（用户 11-03 定）。设备版不走这些
    static let isManager = (Bundle.main.object(forInfoDictionaryKey: "LMCMode") as? String) == "manager"
    /// 假数据预览：不弹系统的指纹和管理员框
    static let isPreview = (Bundle.main.object(forInfoDictionaryKey: "LMCPreview") as? Bool) == true
    /// 这次运行用的语言，传给后端。系统语言不是中文就是英文
    static let uiLanguage = (Bundle.main.preferredLocalizations.first ?? "en").hasPrefix("zh") ? "zh" : "en"
    static var version: String { (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String) ?? "" }
    static let sshBinary = (isPublic ? nil : ProcessInfo.processInfo.environment["LMC_SSH_BIN"]) ?? "/usr/bin/ssh"
    static var sshConfig: String { (isPublic ? nil : ProcessInfo.processInfo.environment["LMC_SSH_CONFIG"]) ?? NSHomeDirectory() + "/.ssh/lmc/config" }
    static var hubAlias: String {
        get { UserDefaults.standard.string(forKey: "hubAlias") ?? "lmc-hub" }
        set { UserDefaults.standard.set(newValue, forKey: "hubAlias") }
    }

    /// 管理版把一条 clusterctl 命令变成给主机的请求；要密码的那两条走专门的入口，密码在标准输入里
    private static func remoteRequest(_ args: [String]) -> [String] {
        if args.first == "chip", args.count >= 2 { return ["chip-" + args[1]] + args.dropFirst(2) }
        if args == ["hub", "state"] { return ["hub-state"] }
        if args.first == "hub", args.count >= 2 {
            if args[1] == "takeover" { return ["hub-takeover"] + (args.contains("--force") ? ["--force"] : []) }
            if args[1] == "handover" || args[1] == "approve", args.count == 3 { return ["hub-handover", args[2]] }
            if args[1] == "reject", args.count == 3 { return ["hub-reject", args[2]] }
            if args[1] == "poweroff", args.count == 3 { return ["hub-poweroff", args[2]] }
        }
        return ["manage"] + args
    }

    static func process(_ args: [String]) -> Process {
        let p = Process()
        if isManager {
            p.executableURL = URL(fileURLWithPath: sshBinary)
            p.arguments = ["-F", sshConfig, "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", hubAlias, (uiLanguage == "en" ? "lang=en " : "") + remoteRequest(args).joined(separator: " ")]
        } else {
            p.executableURL = URL(fileURLWithPath: python)
            p.arguments = ["-B", script] + args
            p.currentDirectoryURL = URL(fileURLWithPath: project)
        }
        var env = ProcessInfo.processInfo.environment
        // 公开版不认测试用的环境变量（体检 R9）：LMC_ROOT 这类能把运行目录、端口、核对都换掉
        if isPublic { for key in env.keys where key.hasPrefix("LMC_") { env.removeValue(forKey: key) } }
        // 从 Dock 打开的应用 PATH 很短；clusterctl 要用 ssh、lsof 等
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["PYTHONIOENCODING"] = "utf-8"
        env["LMC_LANG"] = uiLanguage
        // 3b：更新页比控制台自己的版本（副机的 console 目录里没有 VERSION 文件）
        if let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String { env["LMC_CONSOLE_VERSION"] = v }
        p.environment = env
        p.standardInput = FileHandle.nullDevice
        return p
    }

    /// 一次查询最多等多久（秒）。后端各段自己有超时，这里只是兜底：真挂住时结束子进程，"进行中"才复得了位（体检 A9）。
    /// 宁长勿短：核对安装包、自检、配好后的检查本来就要几分钟
    static func limit(_ args: [String]) -> TimeInterval {
        let second = args.dropFirst().first ?? ""
        switch args.first ?? "" {
        case "requests": return 15           // 后端：远程每台 8 秒、本机 3 秒
        case "status", "progress", "logs": return 180   // 机器多、又都连不上时，逐台的超时加起来也到不了这么久
        case "engine" where ["selfcheck", "open-package", "check-file", "download", "backup-settings"].contains(second): return 900
        case "machine" where second == "check": return 900
        default: return 300
        }
    }

    /// 操作类命令（加载、重启、下载……）多久没有任何新输出算挂住。加载大模型、等请求答完都可能几分钟到十几分钟没有输出，
    /// 所以只看"没动静"的时间，不看总时间，而且放得很宽
    static let idleLimit: TimeInterval = 7200   // 后端自己的超时最长是加载 30 分钟，这里要明显更宽，只兜"真的挂住"

    /// 阻塞执行并返回全部输出；只在后台线程调用。超过时限（见 limit）结束子进程，退出码 -2
    static func run(_ args: [String], input: String? = nil, limit: TimeInterval? = nil) -> (code: Int32, out: String, err: String) {
        let p = process(args)
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        let feed = input.map { _ in Pipe() }
        if let feed = feed { p.standardInput = feed }
        do { try p.run() } catch { return (-1, "", L("无法运行 clusterctl.py：\(error.localizedDescription)")) }
        if let feed = feed, let input = input {
            feed.fileHandleForWriting.write(Data(input.utf8))
            try? feed.fileHandleForWriting.close()
        }
        // 读的线程可能在这里返回之后才读完（见 drain），所以结果放在带锁的盒子里，读一块放一块
        let outData = Locked(Data()), errData = Locked(Data())
        let seconds = limit ?? Backend.limit(args)
        let hung = drain(p, total: seconds, idleSince: nil, { slurp(out, into: outData) }, { slurp(err, into: errData) })
        let text = String(decoding: outData.get(), as: UTF8.self)
        if hung { return (-2, text, L("后端 \(Int(seconds)) 秒没有回应，已结束这次查询。")) }
        if isManager, let line = text.split(separator: "\n").first(where: { $0.hasPrefix("@@not-hub ") }) { followHub(String(line)) }
        return (p.terminationStatus, text, String(decoding: errData.get(), as: UTF8.self))
    }

    /// 读到头，读一块放一块：drain 不再等的时候，已经读到的还在（一次读到头的话，中途放弃就什么都拿不到）
    private static func slurp(_ pipe: Pipe, into box: Locked<Data>) {
        let handle = pipe.fileHandleForReading
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            box.with { $0.append(chunk) }
        }
    }

    /// 两路输出各用一条专用线程读到头，这里等它们读完、子进程退出；都不排进公共线程池。
    /// 以前两路都排进 DispatchQueue.global() 再在这里干等：调用一积压，空出来的线程先被排在前面的调用拿走，
    /// 读输出的活一直排不上，池子的 160 个线程全堵死，操作永远"正在…"、按钮一直灰（踩坑 #63）。
    /// 时限（体检 A9）：total 是总时间，idleSince 给的是"最后一次有输出是什么时候"（超过 idleLimit 算挂住）。
    /// 超了先 TERM，3 秒后 KILL；子进程退了、它留下的孙进程（比如 ssh）还占着管道时，再等 6 秒就不等了，读的线程等孙进程退了自己结束。
    /// 以前没有时限：后端一挂住，"进行中"永远不复位，状态不再更新，只能强退。返回 true 表示是超时结束的
    private static func drain(_ p: Process, total: TimeInterval?, idleSince: (() -> TimeInterval)?, _ readOut: @escaping () -> Void, _ readErr: @escaping () -> Void) -> Bool {
        let done = DispatchSemaphore(value: 0)
        Thread { readErr(); done.signal() }.start()
        Thread { readOut(); done.signal() }.start()
        // 计时都用"开机后醒着的秒数"：按墙上的钟算，机器睡一觉醒来就会把进行中的加载、下载当成超时结束掉
        let started = ProcessInfo.processInfo.systemUptime
        var left = 2, hung = false, killedAt: TimeInterval?, exitedAt: TimeInterval?
        while left > 0 {
            if done.wait(timeout: .now() + 1) == .success { left -= 1; continue }
            let now = ProcessInfo.processInfo.systemUptime
            if !p.isRunning {   // 子进程退了（自己退的、超时结束的、界面上点了停止的），管道还没到头：孙进程占着，再等 6 秒就不等了
                if let at = exitedAt { if now - at > 6 { break } } else { exitedAt = now }
            } else if let at = killedAt {
                if now - at > 3 { kill(p.processIdentifier, SIGKILL) }
            } else if (total.map { now - started > $0 } ?? false) || (idleSince.map { now - $0() > idleLimit } ?? false) {
                hung = true
                killedAt = now
                p.terminate()
            }
        }
        if hung, p.isRunning { kill(p.processIdentifier, SIGKILL) }   // 两路都读到头了，进程却不理 TERM
        p.waitUntilExit()
        return hung
    }

    /// 管理版连的那台不是主机时，对方会告诉谁是主机，改连它
    /// 管理版接管备用机的整个过程不跟：备用机这时还会回"主机是旧的那台"，跟了就又连回已经挂掉的主机
    static var holdAlias = false

    private static func followHub(_ line: String) {
        guard !holdAlias else { return }
        let name = line.replacingOccurrences(of: "@@not-hub ", with: "").trimmingCharacters(in: CharacterSet(charactersIn: "\" "))
        if !name.isEmpty { hubAlias = "lmc-" + name }
    }

    /// 只进剪贴板，30 秒后清掉（如果还没被别的内容替换）
    static func copySecret(_ value: String) {
        DispatchQueue.main.async {
            let board = NSPasteboard.general
            board.prepareForNewContents(with: .currentHostOnly)
            board.setString(value, forType: .string)
            board.setString("", forType: NSPasteboard.PasteboardType("org.nspasteboard.ConcealedType"))
            let stamp = board.changeCount
            DispatchQueue.main.asyncAfter(deadline: .now() + 30) {
                if board.changeCount == stamp { board.clearContents() }
            }
        }
    }

    /// 阻塞执行，逐行回调输出（isError 表示来自 stderr）；只在后台线程调用。
    static func stream(_ args: [String], input: String? = nil, onStart: ((Process) -> Void)? = nil, onLine rawLine: @escaping (String, Bool) -> Void) -> Int32 {
        let p = process(args)
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        let feed = input.map { _ in Pipe() }
        if let feed = feed { p.standardInput = feed }
        do { try p.run() } catch {
            rawLine(L("无法运行 clusterctl.py：\(error.localizedDescription)"), true)
            return -1
        }
        if let feed = feed, let input = input {
            feed.fileHandleForWriting.write(Data(input.utf8))
            try? feed.fileHandleForWriting.close()
        }
        onStart?(p)
        // 两路输出在两条线程上读：回调排成一个一个来（以前两条线程会同时往调用方的数组里写）。last 记最后一次有输出的时间，
        // 给 drain 判断是不是挂住了；closed 之后（这里已经返回）迟到的行不再回调
        let state = Locked((last: ProcessInfo.processInfo.systemUptime, closed: false))
        // 协议行不显示：@@secret 是刚新建的子密钥，只进剪贴板；@@error 是对方的中文错误
        let onLine: (String, Bool) -> Void = { line, isError in state.with { s in
            if s.closed { return }
            s.last = ProcessInfo.processInfo.systemUptime
            if line.hasPrefix("@@secret ") { copySecret(String(line.dropFirst(9))); return }
            if line.hasPrefix("@@not-hub ") { followHub(line); rawLine(L("主机换了，正在改连新主机。"), false); return }
            if line.hasPrefix("@@error ") {
                let text = try? JSONDecoder().decode(String.self, from: Data(line.dropFirst(8).utf8))
                rawLine(text ?? L("操作失败"), true)
                return
            }
            rawLine(line, isError)
        } }
        let pump: (Pipe, Bool) -> Void = { pipe, isError in
            let handle = pipe.fileHandleForReading
            var buffer = Data()
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: 0x0A) {
                    let line = buffer[buffer.startIndex..<newline]
                    buffer.removeSubrange(buffer.startIndex...newline)
                    onLine(String(decoding: line, as: UTF8.self), isError)
                }
            }
            if !buffer.isEmpty { onLine(String(decoding: buffer, as: UTF8.self), isError) }
        }
        let hung = drain(p, total: nil, idleSince: { state.get().last }, { pump(out, false) }, { pump(err, true) })
        if hung {
            onLine(L("后端 \(Int(idleLimit / 60)) 分钟没有任何新输出，已结束这次操作。机器上的动作可能还在继续，过一会儿看状态再决定要不要重做。"), true)
        }
        state.with { $0.closed = true }
        return hung ? -2 : p.terminationStatus
    }
}

/// 带锁的小盒子：几条线程读写同一个值
final class Locked<T> {
    private let lock = NSLock()
    private var value: T
    init(_ value: T) { self.value = value }
    func get() -> T { lock.lock(); defer { lock.unlock() }; return value }
    func set(_ new: T) { lock.lock(); value = new; lock.unlock() }
    @discardableResult func with<R>(_ body: (inout T) -> R) -> R { lock.lock(); defer { lock.unlock() }; return body(&value) }
}

// MARK: - 状态与操作

struct LogLine: Identifiable {
    let id = UUID()
    let text: String
    let isError: Bool
}

final class Console: ObservableObject {
    static weak var shared: Console?

    @Published var status: Status?
    @Published var statusError: String?
    @Published var running: String?
    @Published var runningSince: Date?
    @Published var runningLine = ""     // 这次操作最新的一行输出，显示在顶部进度旁边
    @Published var refreshing = false
    @Published var log: [LogLine] = []
    @Published var lastResult: (ok: Bool, text: String)? { didSet { resultChanged() } }
    /// 结果是几点出来的；那台的状态在这之后变了，结果变灰（它说的已经不是现在了）（T2）
    @Published var resultTime = ""
    @Published var resultStale = false
    private var resultKeys: [String] = []
    private var pendingKeys: [String] = []
    private var resultPoweroff = false, pendingPoweroff = false   // 关机的结果：那台关掉是意料之中
    private var resultAt: Date?
    private var resultStamp: [String: String]?
    /// 底部展开的那块：操作记录是这个控制台做过的事，日志是 oMLX 服务自己的记录；同时只开一个
    enum BottomPanel { case records, logs }
    @Published var panel: BottomPanel? = nil {
        didSet { if (oldValue == nil) != (panel == nil) { resizeForPanel(opening: panel != nil) } }
    }
    @Published var serverLog: [LogLine] = []
    @Published var serverLogLoading = false
    private var panelGrowth: CGFloat = 0
    /// 布局按钮下次排成并排还是上下排
    @Published var fitSideBySide = true
    var barsHeight: CGFloat = 0
    var gridHeight: CGFloat = 0
    /// 打开控制台就启动 oMLX（用户 11-03 定），默认开；设置里能关
    @Published var autoStart = UserDefaults.standard.object(forKey: "autoStart") as? Bool ?? true {
        didSet { UserDefaults.standard.set(autoStart, forKey: "autoStart") }
    }
    /// 重启时"加载回原来的模型"默认勾不勾（用户 11-04 定：默认勾）
    @Published var reloadAfterRestart = UserDefaults.standard.object(forKey: "reloadAfterRestart") as? Bool ?? true {
        didSet { UserDefaults.standard.set(reloadAfterRestart, forKey: "reloadAfterRestart") }
    }
    @Published var bounceDock = UserDefaults.standard.object(forKey: "bounceDock") as? Bool ?? true {
        didSet { UserDefaults.standard.set(bounceDock, forKey: "bounceDock") }
    }
    /// A1（已定第 31 条 ①）：关着时红色按钮只收起窗口；打开后关窗口就是退出，和 ⌘Q 走同一个确认。默认关
    @Published var quitOnClose = UserDefaults.standard.object(forKey: "quitOnClose") as? Bool ?? false {
        didSet { UserDefaults.standard.set(quitOnClose, forKey: "quitOnClose") }
    }
    /// A4：主窗口钉在原处、总在最前；再点一下取消。记住状态
    @Published var pinned = UserDefaults.standard.bool(forKey: "pinWindow") {
        didSet { UserDefaults.standard.set(pinned, forKey: "pinWindow"); applyPin() }
    }
    private var closeHintShown = false   // 这次打开控制台已经提示过怎么完全退出
    /// 这次退出是关窗口引起的：确认框里点了取消，就把窗口开回来
    var quitFromClose = false
    /// 正在启动、关闭、重启的机器 → 卡片上显示什么
    @Published var working: [String: String] = [:]
    /// 自己重启控制台时不弹退出确认
    static var skipExitPrompt = false
    /// 管理版关了主机：连不上是意料之中，提示不一样
    @Published var hubOff = false
    private var startedOnOpen = false
    /// 点过"解锁"的机器：解锁回来后自动启动它的 oMLX
    private var unlocking = Set<String>()
    private var unlockSince: [String: Date] = [:]
    /// "等它答完再卸载"的等待阶段可以停止；clusterctl 一说"开始卸载"就不能停了
    @Published var cancellable = false
    private var current: Process?
    private var cancelled = false
    /// 这次操作最后一次有输出是什么时候。quietLimit 这么久没动静，顶部多一个"强制结束…"；Backend.idleLimit 这么久没动静，后端调用自己结束。
    /// 以前后端一挂住就永远"正在…"，按钮全灰，只能强退应用（体检 A9）
    var lastActivity: Date?
    static let quietLimit: TimeInterval = 1800   // 加载、预热本来就可能十几二十分钟不出声
    private var forced = false

    /// 窗口关掉以后，点 Dock 或菜单栏要能把它重新打开
    var openWindowAction: OpenWindowAction?

    private var timer: Timer?
    private var polling = false
    private var pendingFull = false
    private var lastPoll = Date.distantPast
    private var lastFull = Date.distantPast
    /// 卡住报警：有模型在线时每 60 秒查一次（clusterctl 判断 4 分钟没进度算卡住，最晚约 5 分钟发现）
    @Published var progress: ProgressInfo?
    private var lastProgress = Date.distantPast
    private var progressPolling = false
    // 第 3b 期（草案 10.5–10.8）：更新中心、通知、安装和升级 oMLX。数据都走 updates、engine 的新命令，不进 status
    @Published var updates: UpdatesView?
    @Published var updatesChecking = false
    @Published var showSetupAgain = false   // 教程页点了"再看一遍开始使用"
    @Published var updatesError: String?
    var lastUpdatesCheck = Date.distantPast
    var updatesStarted = false
    @Published var setup = SetupState()
    @Published var settingsTab = SettingsTab.general   // 0.5.2：设置窗口停在哪一页；要去指定的页就先改它再打开
    var setupProcess: Process?
    var setupPrompting = false          // 装好后的确认框开着时，先不问主密钥，免得两个框叠在一起
    @Published var autoUpdate: AutoUpdateInfo?
    @Published var sourceInfo: SourceInfo?
    @Published var extrasError: String?
    /// 设置 → oMLX 一节里改入口、改主密钥态度没成的原因。以前只进主窗口的操作记录，设置窗口里什么都看不到（体检 A14）
    @Published var engineError: String?

    init() {
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        // 切回应用时立刻刷新一次（5 秒内刚刷过就不重复）
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self = self, Date().timeIntervalSince(self.lastFull) > 5 else { return }
            self.refresh(full: true)
        }
        refresh(full: true)
        Console.shared = self
    }

    func node(_ key: String) -> NodeInfo? { status?.nodes?[key] }
    /// 模型的说明：给了机器就先看那台自己的（③存在模型所在的那台），没有再用 status 的 models
    var hasDsh: Bool { status?.tools?.dsh ?? true }
    var hasOllama: Bool { status?.tools?.ollama ?? true }
    func model(_ key: String, on n: String? = nil) -> ModelMeta? { n.flatMap { status?.nodes?[$0]?.meta?[key] } ?? status?.models?[key] }
    func title(_ key: String, on n: String? = nil) -> String { model(key, on: n)?.title ?? key }
    func machine(_ key: String) -> String { node(key)?.title ?? key.capitalized }
    /// 界面文字里的"两台"，按机器数变
    var every: String { nodeOrder.count == 2 ? L("两台") : nodeOrder.count == 1 ? L("这台") : L("\(nodeOrder.count) 台") }
    /// 运行控制台的这台（Ollama、主密钥都看它）
    var localKey: String? { nodeOrder.first { node($0)?.local == true } }

    /// 这台的 oMLX（第 3 期）。自管版没有；管理版是遥控器，不管它连的那台的 oMLX
    var engine: EngineInfo? { Backend.isManager ? nil : status?.engine }
    /// "开始使用"三步（第 4 期）：官方版、没装 oMLX 的机器才有；三步都做完就不显示，教程页能再打开
    var firstRun: SetupInfo? { Backend.isManager ? nil : status?.setup }
    var firstRunVisible: Bool { firstRun.map { !$0.steps.isEmpty && (!$0.done || showSetupAgain) } ?? false }

    /// 客户端连哪个端口：自管版照旧 8800；官方版按 clusterctl 说的（有入口时是入口的）
    var clientPort: Int { engine.flatMap { $0.client_port ?? $0.port } ?? 8800 }

    /// 教程里客户端的地址按这台实际的端口写；自管版照旧 8800，文字一个字不差
    func guideRow(_ row: String) -> String {
        clientPort == 8800 ? row : row.replacingOccurrences(of: "127.0.0.1:8800", with: "127.0.0.1:\(clientPort)")
    }
    var localMachine: String { localKey.map(machine) ?? L("本机") }
    /// 这台控制台是不是主机：全部关闭、关副机都只在主机上有
    var localIsHub: Bool { localKey.flatMap { node($0)?.isHub } == true }
    var anyPowering: Bool { nodeOrder.contains { node($0)?.power == "going_down" } }
    /// 谁能关谁（用户 11-04 定，11-10 放宽）：每台的控制台都能关自己；主机的控制台还能关副机和全部；管理版关每一台和全部。
    /// 别的机器关不了主机（决定 6），后端那边也拦着
    func canPowerOff(_ key: String) -> Bool { Backend.isManager || node(key)?.local == true || (localIsHub && node(key)?.isHub != true) }

    /// 管理版连不上主机时，手上的状态都是旧的
    var stale: Bool { Backend.isManager && statusError != nil }

    /// 这台卡片上模型的顺序：默认新模型靠前，这个控制台自己能调
    func models(on key: String) -> [String] {
        let all = customized(status?.model_recency ?? modelOrder, saved: UserDefaults.standard.stringArray(forKey: "modelOrder." + key), newFirst: true)
        guard let has = node(key)?.has else { return all }
        return all.filter(has.contains)   // 只列这台有的（2b）
    }

    func moveMachine(_ key: String, by delta: Int) {
        guard let i = nodeOrder.firstIndex(of: key), nodeOrder.indices.contains(i + delta) else { return }
        nodeOrder.swapAt(i, i + delta)
        UserDefaults.standard.set(nodeOrder, forKey: "machineOrder")
        UserDefaults.standard.set(nodeOrder, forKey: "nodeOrder")
        objectWillChange.send()
    }

    func moveModel(_ m: String, on key: String, by delta: Int) {
        var list = models(on: key)
        guard let i = list.firstIndex(of: m), list.indices.contains(i + delta) else { return }
        list.swapAt(i, i + delta)
        UserDefaults.standard.set(list, forKey: "modelOrder." + key)
        objectWillChange.send()
    }

    func resetOrder() {
        UserDefaults.standard.removeObject(forKey: "machineOrder")
        for key in nodeOrder { UserDefaults.standard.removeObject(forKey: "modelOrder." + key) }
        if let s = status { nodeOrder = defaultNodeOrder(s) }
        objectWillChange.send()
    }

    /// 机器卡的状态点（用户 11-04 定）：绿＝开机且服务在跑；红＝开着但服务没跑或卡住；灰＝关机或连不上。橙是正在变，转眼就过
    func dot(_ key: String) -> (color: Color, text: String) {
        guard let n = node(key) else { return (.gray, L("检查中…")) }
        if stale { return (.gray, hubOff && n.isHub ? L("已关机") : L("连接不上")) }
        if let doing = working[key] { return (.orange, doing) }
        if n.power == "going_down" { return (.orange, L("正在关机（已 \(Int(Date().timeIntervalSince1970 - (n.power_since ?? Date().timeIntervalSince1970))) 秒）")) }
        if n.power == "stuck" { return (.red, L("关机卡住了")) }
        if !n.reachable {
            if n.waiting_unlock == true { return (.red, L("开着，等你解锁（点“解锁…”）")) }
            return (.gray, n.power == "off" ? L("已关机") : L("连接不上"))
        }
        if n.service == "starting" { return (.orange, L("服务启动中…")) }
        if n.service == "reconnecting" { return (.orange, L("正在重新连接…")) }
        if n.service != "up" { return (.red, L("服务没运行（点“启动”）")) }
        if progress?.state == "stalled" && progress?.node == key { return (.red, L("可能卡住了")) }
        return (.green, L("服务运行中"))
    }

    var anyUp: Bool { nodeOrder.contains { node($0)?.up == true } }
    var anyDown: Bool { nodeOrder.contains { node($0)?.service == "down" && node($0)?.reachable != false && node($0)?.power != "going_down" } }
    var anyLoaded: Bool { nodeOrder.contains { node($0)?.loaded != nil } }
    var anyStarting: Bool { nodeOrder.contains { ["starting", "reconnecting"].contains(node($0)?.service ?? "") } }
    var busy: Bool { running != nil || status?.busy == true }
    var memoryAlert: [String] { nodeOrder.flatMap { node($0)?.alert ?? [] } }
    var overLine: Bool { !memoryAlert.isEmpty }
    var stuck: Bool { progress?.state == "stalled" }

    var menuTitle: String { (overLine ? "⚠️ " : "") + (stuck ? "⏸ " : "") + baseMenuTitle }

    /// 菜单栏：L·DeepSeek  J·MiniMax；各台服务都没开时是 ○ 本地模型
    private var baseMenuTitle: String {
        guard anyUp || anyStarting else { return L("○ 本地模型") }
        return nodeOrder.map { "\(machine($0).prefix(1))·\(shortState($0))" }.joined(separator: "  ")
    }

    func shortState(_ key: String) -> String {
        guard let n = node(key) else { return "…" }
        if n.power == "going_down" { return L("关机中") }
        if !n.reachable { return n.waiting_unlock == true ? L("待解锁") : n.power == "off" ? L("已关机") : L("离线") }
        if n.service == "down" { return L("关") }
        if n.service == "starting" { return L("启动中") }
        if n.service == "reconnecting" { return L("重连中") }
        if n.active.isEmpty { return L("空闲") }
        return n.active.map { (n.models[$0] == "loading" ? "◐" : "") + title($0) }.joined(separator: "+")
    }

    /// 一台的一句话现状
    func sentence(_ key: String) -> String {
        guard let n = node(key) else { return L("检查中…") }
        if !n.reachable { return n.waiting_unlock == true ? L("开着，等待解锁") : n.power == "off" ? L("已关机") : L("连接不上") }
        if n.alert?.isEmpty == false { return L("内存吃紧") }
        if n.service == "down" { return L("oMLX 服务没开") }
        if n.service == "starting" { return L("oMLX 正在启动…") }
        if n.service == "reconnecting" { return L("正在重新连接…") }
        if let m = n.loading { return L("正在加载 \(title(m, on: key))…") }
        if !n.active.isEmpty { return L("正在运行 \(n.active.map { title($0, on: key) }.joined(separator: L("、")))") }
        return L("服务在线，空闲待命")
    }

    // MARK: B2 顶部标题和状态行（已定第 31 条，preview.2 计划 2.2）：单机、主机、副机、管理版四种。菜单栏照旧用 mode_text

    enum HeadLevel {
        case unknown, ok, warn, bad
        var color: Color { switch self { case .ok: return .green; case .warn: return .orange; case .bad: return .red; case .unknown: return .gray } }
    }

    private var hostKey: String? { nodeOrder.first { node($0)?.isHub == true } }
    /// 这台是副机：机器表里它不是主机（副机的窗口只有它自己）
    var localIsWorker: Bool { !Backend.isManager && localKey.flatMap { node($0) }.map { !$0.isHub } == true }

    var headTitle: String { Backend.isManager ? L("管理版") : localKey.map(machine) ?? nodeOrder.first.map(machine) ?? L("本机") }

    var headTag: String {
        if Backend.isManager { return hostKey.map(machine) ?? "" }
        if localIsWorker { return L("副机") }
        return nodeOrder.count > 1 ? L("主机 · \(nodeOrder.count) 台") : L("单机")
    }

    /// 绿＝都正常，橙＝有提醒（内存会超、有机器连不上、服务没开），红＝出问题（内存超线、请求卡住）。具体的提醒照旧在横幅和卡片里
    var headLevel: HeadLevel {
        guard status != nil else { return .unknown }
        if overLine || stuck { return .bad }
        let nodes = nodeOrder.compactMap { node($0) }
        if nodes.contains(where: { $0.warn?.isEmpty == false || (!$0.reachable && $0.power != "off") }) || !nodes.contains(where: { $0.up }) { return .warn }
        return .ok
    }

    /// 一台在跑什么，短说法（标题第二行用）
    private func headState(_ key: String, named: Bool) -> String {
        let name = machine(key)
        guard let n = node(key) else { return L("读取中…") }
        let say = { (text: String) in named ? L("\(name) \(text)") : text }
        if !n.reachable { return say(n.power == "off" ? L("已关机") : L("连不上")) }
        if n.service == "down" { return say(L("oMLX 没在运行")) }
        if n.service == "starting" { return say(L("oMLX 正在启动…")) }
        if n.service == "reconnecting" { return say(L("正在重新连接…")) }
        if let m = n.loading { return say(L("正在加载 \(title(m, on: key))…")) }
        if !n.active.isEmpty { return named ? L("\(name) 跑 \(n.active.map { title($0, on: key) }.joined(separator: L("、")))") : L("在跑 \(n.active.map { title($0, on: key) }.joined(separator: L("、")))") }
        return say(named ? L("空闲") : L("服务在跑，还没加载模型"))
    }

    var headLine: String {
        guard let s = status else { return statusError ?? L("正在读取状态…") }
        let online = nodeOrder.filter { node($0)?.reachable == true }.count
        let time = s.time.map { L("更新于 \($0)") }
        var parts: [String]
        if Backend.isManager {
            parts = [L("遥控 \(nodeOrder.count) 台"), online == nodeOrder.count ? L("都在线") : L("\(online) 台在线")]
        } else if localIsWorker, let key = localKey {
            let host = hub?.enabled == true ? hub?.hub.map(hubTitle) : nil
            parts = [headState(key, named: false), host.map { L("由 \($0) 管理，全局操作在主机上") } ?? L("由主机管理，全局操作在主机上")]
        } else if nodeOrder.count > 1 {
            parts = nodeOrder.map { headState($0, named: true) } + [online == nodeOrder.count ? L("\(online) 台都在线") : L("\(online)/\(nodeOrder.count) 台在线")]
        } else {
            let key = localKey ?? nodeOrder.first
            parts = [key.map { headState($0, named: false) } ?? L("读取中…")]
            if node(key ?? "")?.up == true { parts.append(L("客户端连 127.0.0.1:\(clientPort)")) }
        }
        return (parts + [time].compactMap { $0 }).joined(separator: " · ")
    }

    // MARK: A10 超安全线的原因和改法（已定第 31 条，13.6 第 11 条 ①）

    /// 这台哪个在线模型的上下文让"塞满时"超过安全线，改到哪一档就不超：选项里没标超线、比现在小的最大一档；没有这样的档为 nil。
    /// 后端算 over 时已经把同一台别的在线模型算进去了，所以改到这一档整台就不超
    struct OverFix { let model: String; let now: Int; let fix: Int? }

    func overFixes(_ key: String) -> [OverFix] {
        guard let n = node(key), n.warn?.isEmpty == false else { return [] }
        return n.active.compactMap { m in
            guard let c = n.contexts?[m], let live = c.live, let over = c.over, over.contains(live) else { return nil }
            return OverFix(model: m, now: live, fix: c.options.filter { $0 < live && !over.contains($0) }.max())
        }
    }

    func overText(_ fix: OverFix, on key: String) -> String {
        let n = node(key)
        let most = n?.max_gb.map { L("，塞满约 \($0) GB") } ?? ""
        let line = n?.line_gb.map { L("，超安全线 \($0) GB") } ?? L("，超安全线")
        // A13：这个模型的表放 SSD 能开、现在关着：打开它也能省内存
        let ssd = n?.options?[fix.model]?["ssd"]
        let table = ssd?.mode == "free" && ssd?.on == false ? L("，或打开「表放 SSD」") : ""
        return L("\(title(fix.model, on: key)) 上下文 \(contextLabel(fix.now))\(most)\(line)；")
            + (fix.fix.map { L("改成 \(contextLabel($0)) 以内就不超") } ?? L("上下文调到最小也超：卸载别的模型，或换一个小一点的模型")) + table
    }

    private var mainWindow: NSWindow? { NSApp.windows.first { Self.isMain($0) && $0.canBecomeMain } }
    static func isMain(_ w: NSWindow) -> Bool { w.identifier?.rawValue == "main" || w.title == L("ModelTower") }

    func showMainWindow() {
        NSApp.activate(ignoringOtherApps: true)
        if let window = mainWindow {
            window.makeKeyAndOrderFront(nil)
        } else {
            openWindowAction?(id: "main")
        }
        DispatchQueue.main.async { self.applyPin() }
    }

    /// A4：钉住＝窗口浮在别的窗口上面、拖不动；取消就回到普通窗口
    func applyPin() {
        for window in NSApp.windows { matchPinLevel(window) }
        guard let window = mainWindow else { return }
        window.level = pinned ? .floating : .normal
        window.isMovable = !pinned
    }

    /// 钉住时主窗口浮在别的窗口上面；自己的别的窗口（设置、安装和升级、管理页）要跟着浮起来，不然一打开就被主窗口挡在后面
    func matchPinLevel(_ window: NSWindow) {
        guard !(window is NSPanel), window.styleMask.contains(.titled), !Self.isMain(window), window.sheetParent == nil else { return }
        let level: NSWindow.Level = pinned ? .floating : .normal
        if window.level != level { window.level = level }
    }

    /// A1：主窗口被红色按钮关掉以后。开关打开时就是退出（和 ⌘Q 同一个确认）；关着时这次打开控制台第一次关窗口提示一次怎么完全退出
    func mainWindowClosed() {
        guard !AppDelegate.quitting, !Console.skipExitPrompt else { return }
        if quitOnClose {
            quitFromClose = true
            DispatchQueue.main.async { NSApp.terminate(nil) }
            return
        }
        guard !closeHintShown, !UserDefaults.standard.bool(forKey: "closeHintOff") else { return }
        closeHintShown = true
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.messageText = Backend.isManager ? L("窗口收起了，管理版还开着") : L("窗口收起了，控制台还在运行")
            alert.informativeText = (Backend.isManager ? L("菜单栏的图标继续显示各台的状态。") : L("oMLX 和加载着的模型照常运行，菜单栏的图标继续显示状态。"))
                + L("点 Dock 图标就能把窗口开回来。\n\n要完全退出：菜单栏图标 → 退出控制台，或按 ⌘Q。想点红色按钮就退出，到设置 → 通用打开「关闭窗口时退出控制台」。")
            alert.showsSuppressionButton = true
            alert.suppressionButton?.title = L("不再提示")
            alert.addButton(withTitle: L("好"))
            _ = Console.present(alert)
            if alert.suppressionButton?.state == .on { UserDefaults.standard.set(true, forKey: "closeHintOff") }
        }
    }

    // MARK: 刷新
    // 快速刷新只看各台的服务和模型（远程机器经隧道问 HTTP，不走 SSH）；完整刷新再走一次 SSH 取远程机器的内存、
    // 硬盘，并在隧道断了时接上。平时快速每 10 秒、完整每 60 秒（有模型在线时也 10 秒，各台的内存都 10 秒一更）；
    // 操作进行中或服务正在启动时快速每 2 秒、完整每 10 秒。

    private func tick() {
        guard Backend.pythonPath != nil else { return }   // 还没有能用的 Python（主窗口是说明页）：不空跑
        let now = Date()
        let active = busy || anyStarting || !unlocking.isEmpty || anyPowering
        let fullEvery: TimeInterval = active || anyLoaded ? 10 : 60
        let fastEvery: TimeInterval = active ? 2 : 10
        if now.timeIntervalSince(lastFull) >= fullEvery { refresh(full: true) }
        else if now.timeIntervalSince(lastPoll) >= fastEvery { refresh(full: false) }
        if now.timeIntervalSince(lastHubCheck) >= 60 && running == nil { checkHub() }
        if !anyLoaded { if progress != nil { progress = nil } }
        else if now.timeIntervalSince(lastProgress) >= 60 { checkProgress() }
        for (key, since) in unlockSince where now.timeIntervalSince(since) > 600 {   // 点过"解锁…"没解成：10 分钟后不再按"活跃"高频刷新
            unlocking.remove(key)
            unlockSince[key] = nil
        }
        if updatesStarted && now.timeIntervalSince(lastUpdatesCheck) >= 6 * 3600 { checkUpdates(.regular) }
    }

    /// 卡住时 clusterctl 自己发一次系统通知；这里负责橙条、菜单栏 ⏸ 和 Dock 图标跳动
    func checkProgress() {
        guard !progressPolling else { return }
        progressPolling = true
        lastProgress = Date()
        DispatchQueue.global().async {
            let result = Backend.run(["progress", "--json"])
            let decoded = try? JSONDecoder().decode(ProgressInfo.self, from: Data(result.out.utf8))
            DispatchQueue.main.async {
                self.progressPolling = false
                guard let p = decoded else { return }
                let wasStuck = self.stuck
                self.progress = p
                if self.stuck && !wasStuck && self.bounceDock { NSApp.requestUserAttention(.criticalRequest) }
            }
        }
    }

    /// 读到状态以后：打开控制台时启动一次 oMLX；副机自己回来、解锁回来的也启动（D1）。都不自动加载模型
    private func afterStatus() {
        if Backend.isManager {
            for key in Array(unlocking) where node(key)?.reachable == true {
                unlocking.remove(key)
                if node(key)?.service == "down" { startService(key) }
            }
        } else { cameBack(); watchInstall(); askEngineKey(); startUpdates() }
        guard !startedOnOpen else { return }
        startedOnOpen = true
        if autoStart && !Backend.isManager && (anyDown || nodeOrder.count > 1) { perform(L("启动 oMLX"), ["autostart"]) }
        else if engine?.official == true && engine?.gateway == "stopped" { runGateway("ensure") }
    }

    /// D1（用户 11-10 定，已定第 17 条）：副机从连不上变成开着、服务没运行，而且关机前 oMLX 在跑（clusterctl 记的 wants_up；
    /// 点过"解锁…"也记成要开），主机自动启动它的 oMLX，不加载模型。要主机的控制台开着、设置里"打开控制台时启动 oMLX"开着
    /// （点过解锁的不看这个开关：按钮上答应过）、没有操作在跑；有操作就排着，等它做完。运行窗口被占用时 clusterctl 不启动。
    /// 只看完整刷新：快速刷新不走 SSH，不知道连不连得上。记录在文件里，应用重开也还在
    private var lastReachable: [String: Bool] = [:]
    private var comeBack: [(key: String, unlocked: Bool)] = []

    private func cameBack() {
        for key in nodeOrder {
            guard let n = node(key), n.local != true, n.checked else { continue }
            if lastReachable[key] == false, n.reachable, n.service == "down", n.wants_up == true, !comeBack.contains(where: { $0.key == key }) {
                comeBack.append((key, unlocking.contains(key)))
            }
            lastReachable[key] = n.reachable
            if n.reachable { unlocking.remove(key) }
        }
        guard running == nil, localIsHub, !comeBack.isEmpty else { return }
        let next = comeBack.removeFirst()
        guard autoStart || next.unlocked, let n = node(next.key), n.reachable, n.service == "down" else { return }
        let done = track(next.key, L("服务启动中…"))
        perform(L("启动 \(machine(next.key))"), ["autostart", next.key], summary: true, onDone: done)
    }

    /// 官方版第一次对接时问一次允不允许读主密钥（草案 10.3）。答过（允许或不允许）就不再问，设置里能改
    private var askedEngineKey = false
    var engineUnsigned = false   // 装好后核出不是官方签名：不问主密钥

    private func askEngineKey() {
        guard !askedEngineKey, !setupPrompting, !engineUnsigned, let e = engine, e.official, e.key == "ask", running == nil, !Backend.isPreview else { return }
        askedEngineKey = true
        DispatchQueue.main.async { self.askEngineKeyNow() }
    }

    func askEngineKeyNow() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = L("允许控制台读这台 oMLX 的主密钥吗？")
        alert.informativeText = L("这台的 oMLX 是官方版。控制台要读它的主密钥，才能加载和卸载模型、管理子密钥、看日志和多轮缓存。\n\n控制台只记你允许还是不允许，不存密钥，每次用时现读；只有升级 oMLX 前会给它的设置文件留一份备份（里面有主密钥），放在这台、只有你的账号能读。不允许的话只能看状态，以后可以在设置的 oMLX 页改。")
        alert.addButton(withTitle: L("允许"))
        alert.addButton(withTitle: L("不允许"))
        setEngineKey(Console.present(alert) == .alertFirstButtonReturn)
    }

    /// 入口（已定第 29 条 ②）：官方版的子密钥能点起没加载的模型，入口只放行已加载的。关之前说清后果
    func setGateway(_ on: Bool) {
        guard on != (engine?.gateway != "off") else { return }
        if !on {
            guard confirm(L("关掉入口？"),
                          L("关掉以后，客户端要改连 oMLX 的端口 \(engine?.port.map { String($0) } ?? "")。官方 oMLX 的子密钥能直接加载模型：客户端点到一个没加载的模型，它会被加载起来，可能挤掉正在用的模型。\n\n随时能在这里再打开。"),
                          ok: L("关掉入口")) else { objectWillChange.send(); return }
        }
        runGateway(on ? "on" : "off")
    }

    func runGateway(_ what: String) {
        guard !Backend.isManager, !Backend.isPreview else { return }
        DispatchQueue.global().async {
            let r = Backend.run(["engine", "gateway", what, "--json"])
            struct Reply: Decodable { var ok: Bool?; var note: String?; var error: String?; var result: String? }
            let reply = lastJSON(r.out, as: Reply.self)
            DispatchQueue.main.async {
                // 没成有两种：后端报错；或者回了 ok、但入口没起来（result 是 port_busy、failed，原因在 note 里）
                let failed = reply?.ok != true || ["port_busy", "failed"].contains(reply?.result ?? "")
                let text = failed ? (reply?.note ?? reply?.error ?? failureText(r)) : reply?.note
                // 打开控制台时自动补起的那次（ensure）顺利就不进记录，和以前一样
                if what != "ensure" || text != nil { self.append("▶ " + (what == "on" ? L("打开入口") : what == "off" ? L("关掉入口") : L("启动入口")), false) }
                if let text { self.append(text, failed) }
                self.engineError = failed ? text : nil   // 设置窗口里也写一行
                self.refresh(full: true)
            }
        }
    }

    /// 只存态度，很快：不占操作锁（不走 perform），加载等操作进行中也能改
    func setEngineKey(_ allow: Bool) {
        let label = allow ? L("允许读主密钥") : L("不允许读主密钥")
        DispatchQueue.global().async {
            let r = Backend.run(["engine", allow ? "allow-key" : "deny-key"])
            DispatchQueue.main.async {
                self.append("▶ \(label)", false)
                let text = (r.code == 0 ? r.out : r.err).trimmingCharacters(in: .whitespacesAndNewlines)
                if !text.isEmpty { self.append(text, r.code != 0) }
                self.engineError = r.code == 0 ? nil : L("\(label)没成：\(text.isEmpty ? failureText(r) : text.replacingOccurrences(of: "✕ ", with: ""))")
                self.refresh(full: true)
            }
        }
    }

    /// T2：结果出来后第一次读到的状态记下来；之后那几台的状态变了，结果就变灰
    private func resultChanged() {
        resultStale = false
        resultStamp = nil
        guard lastResult != nil else { resultTime = ""; resultAt = nil; return }
        resultAt = Date()
        resultKeys = pendingKeys
        resultPoweroff = pendingPoweroff
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        resultTime = f.string(from: Date())
    }

    private func signature(_ key: String) -> String {
        guard let n = node(key) else { return "" }
        if resultPoweroff { return n.reachable && n.power == nil ? "back" : "off" }   // 关机之后只看它回没回来
        return "\(n.reachable)|\(n.service)|\(n.power ?? "")|" + modelOrder.map { n.models[$0] ?? "off" }.joined(separator: ",")
    }

    private func stampResult(since started: Date) {
        guard let at = resultAt, lastResult != nil, !resultStale else { return }
        let now = Dictionary(uniqueKeysWithValues: resultKeys.map { ($0, signature($0)) })
        if let old = resultStamp { if old != now { resultStale = true } }
        else if started >= at { resultStamp = now }
    }

    func refresh(full: Bool = true) {
        guard !polling else {
            if full { pendingFull = true }
            return
        }
        polling = true
        lastPoll = Date()
        if full {
            lastFull = Date()
            refreshing = true
        }
        let args = ["status", "--json"] + (full ? [] : ["--no-worker"])
        let started = Date()
        DispatchQueue.global().async {
            let result = Backend.run(args)
            let decoded = try? JSONDecoder().decode(Status.self, from: Data(result.out.utf8))
            DispatchQueue.main.async {
                self.polling = false
                self.refreshing = false
                if var s = decoded, s.ok {
                    // 快速刷新没走 SSH：远程机器的内存沿用上次；隧道断着时（这次说"没开"）整台沿用上次，等完整刷新接上
                    if !full {
                        for (key, new) in s.nodes ?? [:] where !new.checked {
                            if let old = self.status?.nodes?[key] {
                                // 隧道断着时整台沿用上次，但关机的记号用这次的：不然"正在关机"会被上次的状态盖掉（D4）
                                var kept = new.service == "down" ? old : new.keepingMachine(from: old)
                                kept.power = new.power; kept.power_since = new.power_since; kept.power_fresh = new.power_fresh
                                s.nodes?[key] = kept
                            }
                        }
                    }
                    let wasOver = self.overLine
                    if let order = s.model_order, !order.isEmpty { modelOrder = order; UserDefaults.standard.set(order, forKey: "lastModelOrder") }
                    tintOrder = s.order ?? []
                    let order = customized(defaultNodeOrder(s), saved: UserDefaults.standard.stringArray(forKey: "machineOrder"))
                    if !order.isEmpty, order != nodeOrder {
                        nodeOrder = order
                        UserDefaults.standard.set(order, forKey: "nodeOrder")
                    }
                    self.status = s
                    self.statusError = nil
                    self.hubOff = false
                    self.stampResult(since: started)
                    self.afterStatus()
                    // 刚变红时让 Dock 图标跳一下（应用在前台时系统不跳）；一直红着不重复提醒
                    if self.overLine && !wasOver && self.bounceDock { NSApp.requestUserAttention(.informationalRequest) }
                } else {
                    let detail = decoded?.error ?? result.err.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.statusError = detail.isEmpty ? L("读取状态失败") : detail
                }
                if self.pendingFull {
                    self.pendingFull = false
                    self.refresh(full: true)
                }
            }
        }
    }

    /// 执行一个操作：逐行显示输出；失败时把命令行给出的中文错误原样显示。
    /// summary 为 true 时，把最后一行输出当作结果（自检、体检用）。
    func perform(_ label: String, _ args: [String], showLog: Bool = false, summary: Bool = false, cancellable: Bool = false,
                 input: String? = nil, onError: ((String) -> Void)? = nil, onDone: ((Bool) -> Void)? = nil) {
        guard running == nil else {
            if input != nil { DispatchQueue.main.async { self.notify(L("有操作正在进行，这次没有做"), L("等它做完再试。")) } }
            else { DispatchQueue.main.async { self.append(L("\(label)：这次没有做，有别的操作正在进行，等它做完再试。"), true) } }
            onDone?(false)
            return
        }
        running = label
        runningSince = Date()
        runningLine = ""
        lastActivity = Date()
        forced = false
        pendingKeys = nodeOrder.filter { args.contains($0) }
        if pendingKeys.isEmpty || args.contains("all") { pendingKeys = nodeOrder }
        pendingPoweroff = args.first == "poweroff" || args.prefix(2) == ["hub", "poweroff"]
        lastResult = nil
        cancelled = false
        self.cancellable = cancellable
        if showLog { panel = .records }
        append("▶ \(label)", false)
        var errors: [String] = []
        var lastOut = ""
        var goingDown = false
        var doctorFailed = false   // 体检有没过的项：后端打 @@doctor-failed，不再认"要看"两个字
        DispatchQueue.global().async {
            let code = Backend.stream(args, input: input, onStart: { p in DispatchQueue.main.async { self.current = p } }) { line, isError in
                if line == "@@going-down" { goingDown = true; return }
                if line == "@@unloading" { DispatchQueue.main.async { self.cancellable = false }; return }   // 后端说卸载开始了，不能再停
                if line == "@@doctor-failed" { doctorFailed = true; return }
                if isError { errors.append(line) }
                else if !line.trimmingCharacters(in: .whitespaces).isEmpty { lastOut = line }
                DispatchQueue.main.async {
                    self.lastActivity = Date()
                    if !isError && !line.trimmingCharacters(in: .whitespaces).isEmpty { self.runningLine = line }
                    self.append(line, isError)
                }
            }
            DispatchQueue.main.async {
                let seconds = Int(Date().timeIntervalSince(self.runningSince ?? Date()))
                self.running = nil
                self.runningSince = nil
                self.current = nil
                self.cancellable = false
                let message = errors.last(where: { !$0.trimmingCharacters(in: .whitespaces).isEmpty }) ?? ""
                // 关的是连着的这台时，连接随关机断开，ssh 报 255；对方事先说过要关机才算成功
                let finished = code == 0 || (code == 255 && goingDown)
                if code != 0 && self.forced {
                    let text = L("\(label)：已强制结束，控制台不再等。机器上的动作可能还在继续，看一下状态再决定要不要重做。")
                    self.lastResult = (false, text)
                    self.append(text, true)
                } else if code != 0 && self.cancelled {
                    self.lastResult = (true, L("\(label)：已停止等待，什么都没改"))
                } else if finished {
                    let text = lastOut.replacingOccurrences(of: "● ", with: "").replacingOccurrences(of: "▲ ", with: "")
                    self.lastResult = summary && !text.isEmpty ? (!doctorFailed, L("\(label)：\(text)"))
                                                                : (true, L("\(label)：完成（\(seconds) 秒）"))
                } else {
                    let text = message.replacingOccurrences(of: "✕ ", with: "")
                    self.lastResult = (false, text.isEmpty ? L("\(label)：失败（退出码 \(code)）") : text)
                    self.panel = .records  // 失败时把详情摊开
                    onError?(errors.joined(separator: "\n"))
                }
                self.refresh(full: true)
                onDone?(finished)
            }
        }
    }

    /// 停止"等它答完再卸载"的等待：这时还什么都没动；clusterctl 开始卸载后会忽略这个信号
    func stopWaiting() {
        guard cancellable, let p = current else { return }
        cancelled = true
        p.terminate()
    }

    /// 操作很久没有任何输出时的出口：只是让控制台不再等。先说清后果再结束子进程
    func forceStop() {
        guard let label = running, let p = current else { return }
        let minutes = Int(Date().timeIntervalSince(lastActivity ?? Date()) / 60)
        guard confirm(L("强制结束「\(label)」？"),
                      L("它已经 \(minutes) 分钟没有任何新输出。强制结束只是让控制台不再等：机器上的动作可能还在继续，也可能停在半路（比如模型只加载了一半）。结束后先看状态，再决定要不要重做。"),
                      ok: L("强制结束"), critical: true) else { return }
        guard running == label, current === p else { return }   // 确认框开着时它自己做完了
        forced = true
        p.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 3) { if p.isRunning { kill(p.processIdentifier, SIGKILL) } }
    }

    private func append(_ text: String, _ isError: Bool) {
        log.append(LogLine(text: text, isError: isError))
        if log.count > 400 { log.removeFirst(log.count - 400) }
    }

    func clearLog() { log.removeAll() }

    // MARK: 用户操作（确认对话框都在这里，菜单栏、长条和窗口共用）。node 为 nil 表示各台

    /// 启动、关闭、重启都带上副机的控制台（用户 11-04 定）。这几个操作前问一下那台此刻有没有人在用
    private func usage(_ keys: [String]) -> String {
        let up = keys.filter { node($0)?.up == true }
        let counts = requestCounts(up)
        let lines = up.compactMap { key -> String? in
            guard let c = counts[key] else { return L("读不到 \(machine(key)) 此刻的请求数。") }
            return c.active + c.waiting > 0 ? L("\(machine(key)) 正在处理 \(c.active) 个请求、排队 \(c.waiting) 个，会被中断。") : nil
        }
        return lines.isEmpty ? L("此刻没有请求在处理。") : lines.joined(separator: "\n")
    }

    private func track(_ node: String?, _ text: String) -> (Bool) -> Void {
        let keys = node.map { [$0] } ?? nodeOrder
        for key in keys { working[key] = text }
        return { [weak self] _ in for key in keys { self?.working[key] = nil } }
    }

    func startService(_ node: String? = nil) {
        guard running == nil else { return }
        let done = track(node, L("服务启动中…"))
        perform(node.map { L("启动 \(machine($0))") } ?? L("启动全部"), ["up"] + (node.map { [$0] } ?? []), onDone: { [weak self] ok in
            done(ok)
            self?.noteDesktop(node.map { [$0] } ?? nodeOrder, ok)
        })
    }

    /// T1：副机停在登录界面时，它的控制台窗口打不开（oMLX 照常能用），结果后面说一句
    private func noteDesktop(_ keys: [String], _ ok: Bool) {
        guard ok, let r = lastResult else { return }
        let names = keys.filter { node($0)?.local != true && node($0)?.desktop == false }.map(machine)
        guard !names.isEmpty else { return }
        lastResult = (r.ok, L("\(r.text)。\(names.joined(separator: L("、"))) 没登录桌面，它的控制台窗口没打开，服务照常能用。"))
    }

    /// 重启：确认框里能选要不要把模型加载回来，默认按设置
    func restartService(_ node: String? = nil) {
        guard running == nil else { return }
        let keys = node.map { [$0] } ?? nodeOrder.filter { self.node($0)?.up == true }
        let loaded = keys.flatMap { k in (self.node(k)?.active ?? []).map { L("\(machine(k)) 的 \(title($0))") } }.joined(separator: L("、"))
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = node.map { L("重启 \(machine($0))？") } ?? L("重启全部？")
        alert.informativeText = (node == nil ? L("所有机器的 oMLX 都会重启，副机的控制台重开，这台的控制台刷新。") : L("\(machine(node!)) 的 oMLX 和控制台都会重启。")) + "\n\n" + usage(keys)
        alert.alertStyle = .warning
        if !loaded.isEmpty {
            alert.showsSuppressionButton = true
            alert.suppressionButton?.title = L("重启后加载回 \(loaded)（每个约 1–3 分钟）")
            alert.suppressionButton?.state = reloadAfterRestart ? .on : .off
        }
        alert.addButton(withTitle: L("重启"))
        alert.addButton(withTitle: L("取消"))
        guard Console.present(alert) == .alertFirstButtonReturn else { return }
        // 重启的是这台时，控制台做完会自己重开
        if !Backend.isManager, let node = node, node == localKey,
           !mayInterruptSetup(L("重启这台时控制台会重开，会把它打断。下了的部分留着，下次接着下。"), ok: L("仍然重启")) { return }
        let reload = !loaded.isEmpty && alert.suppressionButton?.state == .on
        if !loaded.isEmpty { reloadAfterRestart = reload }   // 记住这次的选择，下次默认一样
        let done = track(node, L("正在重启…"))
        perform(node.map { L("重启 \(machine($0))") } ?? L("重启全部"), ["cycle", node ?? "all", "--yes"] + (reload ? ["--reload"] : []), onDone: { [weak self] ok in
            done(ok)
            if ok, let node = node, node == self?.localKey, !Backend.isManager { self?.relaunch() }
        })
    }

    /// 语言：""是跟随系统。写这个应用自己的 AppleLanguages，重开才生效；重开不停服务，模型不受影响
    var language: String { (UserDefaults.standard.persistentDomain(forName: Bundle.main.bundleIdentifier ?? "")?["AppleLanguages"] as? [String])?.first ?? "" }

    func setLanguage(_ code: String) {
        guard code != language else { return }
        guard running == nil else {
            notify(L("有操作正在进行"), L("等它做完再切换语言。"))
            objectWillChange.send()
            return
        }
        guard confirm(L("切换语言要重开控制台"), L("窗口会关掉再打开。服务和模型不受影响。"), ok: L("重开")),
              mayInterruptSetup(L("现在重开控制台会把它打断。下了的部分留着，下次接着下。"), ok: L("仍然重开")) else { objectWillChange.send(); return }
        // 确认框开着的时候可能有操作自动开始了（解锁回来的机器启动 oMLX、打开时的自动启动）：重开会把它打断，这次不切
        if let busy = running {
            notify(L("「\(busy)」刚开始，这次没有切换语言"), L("等它做完再切换语言。"))
            objectWillChange.send()
            return
        }
        applyLanguage(code)
        relaunch()
    }

    /// 安装、升级 oMLX 的窗口里有事在做（下载、核对、备份、自检，不占操作锁）时，重开或退出控制台会把它打断：先问一句。
    /// 以前切语言、重启本机、⌘Q 都不看它，下载说断就断（能续传，不丢数据；体检 A16）
    func mayInterruptSetup(_ text: String, ok: String) -> Bool {
        guard let busy = setup.busy else { return true }
        return confirm(L("oMLX 的安装还在进行"), busy + "\n\n" + text, ok: ok)
    }

    func applyLanguage(_ code: String) {
        if code.isEmpty { UserDefaults.standard.removeObject(forKey: "AppleLanguages") } else { UserDefaults.standard.set([code], forKey: "AppleLanguages") }
        UserDefaults.standard.synchronize()
    }

    /// 本机的控制台自己重开：等这个进程退了再打开
    func relaunch() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        // 等这个进程真的退了再打开（最多等 20 秒）。以前固定等 1 秒：退得慢时 open 只是把还没退完的旧进程叫到前面，关掉后就不再打开了
        p.arguments = ["-c", "i=0; while kill -0 \"$1\" 2>/dev/null && [ $i -lt 100 ]; do sleep 0.2; i=$((i+1)); done; sleep 0.5; /usr/bin/open \"$0\"",
                       Bundle.main.bundlePath, String(ProcessInfo.processInfo.processIdentifier)]
        try? p.run()
        Console.skipExitPrompt = true
        NSApp.terminate(nil)
    }

    /// 一台的"关闭"。副机本机：控制台和 oMLX 同进退，就是退出，走退出确认。主机本机：只停 oMLX，窗口留着还能点启动。
    func stopService(_ key: String) {
        guard running == nil else { return }
        let isLocal = node(key)?.local == true
        if isLocal && !localIsHub && !Backend.isManager { NSApp.terminate(nil); return }
        let what = isLocal ? L("会卸载模型、停止 oMLX。窗口留着，可以再点启动。") : L("会卸载模型、停止 oMLX，它的控制台也一起退出。主机这边还能再点启动。")
        guard confirm(L("关闭 \(machine(key))？"), what + "\n\n" + usage([key]), ok: L("关闭")) else { return }
        let done = track(key, L("服务关闭中…"))
        perform(L("关闭 \(machine(key))"), ["down", key, "--yes"], onDone: done)
    }

    /// 关闭全部：主机是唯一入口，这和退出主机的控制台是一回事（用户 11-04 定）。管理版只是遥控器，窗口留着
    func closeAll() {
        guard running == nil else { return }
        guard Backend.isManager else { NSApp.terminate(nil); return }
        let keys = nodeOrder
        guard confirm(L("关闭全部？"), L("所有机器的 oMLX 和副机的控制台都会关闭，模型卸载，机器本身不关机。\n\n") + usage(keys), ok: L("关闭全部")) else { return }
        let done = track(nil, L("服务关闭中…"))
        perform(L("关闭全部"), ["down", "all", "--yes"], onDone: done)
    }

    func unloadAll() {
        let names = nodeOrder.flatMap { n in (node(n)?.active ?? []).map { L("\(machine(n)) 的 \(title($0))") } }.joined(separator: L("、"))
        guard !names.isEmpty, confirm(L("卸载\(every)的全部模型？"), L("会卸载 \(names)，释放内存；\(every)的 oMLX 服务保持运行。之后要用时重新加载。"),
                                      ok: L("全部卸载")) else { return }
        perform(L("卸载全部模型"), ["unload", "all"])
    }

    func unload(_ m: String, on n: String) {
        let choice = confirmUnload(n, L("卸载 \(machine(n)) 上的 \(title(m, on: n))？"), L("释放 \(machine(n)) 的内存，服务保持运行。"), ok: L("卸载"))
        guard choice != .cancel else { return }
        perform(L("卸载 \(machine(n)) 的 \(title(m, on: n))"), ["unload", m, "--on", n] + (choice == .wait ? ["--wait"] : []),
                cancellable: choice == .wait)
    }

    /// 这台空着，或放得下（status 的 fits）就直接加载，别的模型继续跑；放不下、或点的是"切换"才问，先卸载别的
    func load(_ key: String, on n: String, replace: Bool = false) {
        guard let meta = model(key, on: n) else { return }
        let others = (node(n)?.active ?? []).filter { $0 != key }.map { title($0, on: n) }
        if others.isEmpty || (!replace && node(n)?.fits?[key] == true) {
            runLoad(key, n, meta, free: false, mode: others.isEmpty ? [] : ["--keep"])
            return
        }
        let current = others.joined(separator: L("、"))
        let choice = confirmUnload(n, L("在 \(machine(n)) 上切换到 \(meta.title)？"),
                                   replace ? L("会先卸载 \(current)，再加载 \(meta.title)（\(meta.eta)）。")
                                           : L("\(machine(n)) 放不下它们一起跑。会先卸载 \(current)，再加载 \(meta.title)（\(meta.eta)）。"),
                                   ok: L("卸载 \(current) 并加载"))
        guard choice != .cancel else { return }
        runLoad(key, n, meta, free: false, wait: choice == .wait, mode: ["--replace"])
    }

    enum UnloadChoice { case now, wait, cancel }

    /// 卸载、换模型前的确认：先问一次那台此刻有没有请求在处理（约 0.3 秒），有就多给一个"等它答完再卸载"。
    private func confirmUnload(_ n: String, _ title: String, _ text: String, ok: String) -> UnloadChoice {
        let counts = requestCounts([n])[n]
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.alertStyle = .warning
        guard let c = counts, c.active + c.waiting > 0 else {
            if counts == nil {   // 读不到请求数：也给"等它答完"，后端等的时候自己会再问（审查 11-15）
                alert.informativeText = text + L("\n\n读不到 \(machine(n)) 此刻的请求数；正在用它的对话会中断。")
                    + L("选「等它答完再卸载」会等到没有请求时再动手（最长 10 分钟，等的时候可以停止）。")
                alert.addButton(withTitle: L("等它答完再卸载"))
                alert.addButton(withTitle: ok)
                alert.addButton(withTitle: L("取消"))
                switch Console.present(alert) {
                case .alertFirstButtonReturn: return .wait
                case .alertSecondButtonReturn: return .now
                default: return .cancel
                }
            }
            alert.informativeText = text + L("\n\n\(machine(n)) 此刻没有请求在处理，不会打断谁。")
            alert.addButton(withTitle: ok)
            alert.addButton(withTitle: L("取消"))
            return Console.present(alert) == .alertFirstButtonReturn ? .now : .cancel
        }
        alert.informativeText = text + L("\n\n\(machine(n)) 此刻正在回答 \(c.active) 个请求") + (c.waiting > 0 ? L("、排队 \(c.waiting) 个") : "")
            + L("，现在卸载会中断它们。选「等它答完再卸载」会等到没有请求时再动手（最长 10 分钟，等的时候可以停止）。")
        alert.addButton(withTitle: L("等它答完再卸载"))
        alert.addButton(withTitle: L("现在就卸载（中断）"))
        alert.addButton(withTitle: L("取消"))
        switch Console.present(alert) {
        case .alertFirstButtonReturn: return .wait
        case .alertSecondButtonReturn: return .now
        default: return .cancel
        }
    }

    private func runLoad(_ key: String, _ n: String, _ meta: ModelMeta, free: Bool, wait: Bool = false, mode: [String] = []) {
        perform(L("在 \(machine(n)) 上加载 \(meta.title)"), ["start", key, "--on", n, "--yes"] + mode + (free ? ["--free-ollama"] : []) + (wait ? ["--wait"] : []),
                cancellable: wait) { errors in
            guard errors.contains("--free-ollama") else { return }
            if self.confirm(L("让 Ollama 先释放内存？"), L("Ollama 正占着 \(self.localMachine) 的内存，加上 \(meta.title) 会超过上限。确认后会请 Ollama 卸载它当前的模型，再加载 \(meta.title)。"),
                            ok: L("释放并加载")) {
                self.runLoad(key, n, meta, free: true, mode: mode)
            }
        }
    }

    /// 模型开关（MTP、表放 SSD）。两项都不能在模型运行中直接切换，要重新加载这个模型：只下线这台的它，
    /// oMLX 服务、控制台和 dsh 都不用重启。没在跑的直接保存，不弹窗；在跑的只有"切换 / 取消"（11-02 用户定）：
    /// 切换就是当场卸载、重新加载，不等请求答完；弹窗写明此刻有多少请求会被中断，想保住就取消、过会儿再拨。
    /// "稍后"只留在命令行（--later）。
    func setOption(_ opt: String, _ m: String, on n: String, _ value: Bool) {
        guard let meta = model(m, on: n) else { return }
        let label = opt == "mtp" ? "MTP" : L("表放 SSD"), word = value ? L("开") : L("关")
        let args = ["option", n, m, opt, value ? "on" : "off"]
        guard node(n)?.models[m] == "loaded" else {
            perform(L("设置 \(label)"), args, showLog: false, summary: true)
            return
        }
        var text = L("\(label) 不能在模型运行中直接切换，要重新加载 \(meta.title) 才生效：\(machine(n)) 上的它会下线\(meta.eta)")
            + (hasDsh ? L("（卸载 → 改设置 → 加载 → 自检），oMLX 服务、控制台和 dsh 都不用重启。") : L("（卸载 → 改设置 → 加载 → 自检），oMLX 服务和控制台都不用重启。"))
        if meta.warmup == true && opt == "mtp" && value {
            text += L("\n\n升级系统或 oMLX 后第一次开，加载后还会自动预热 1–3 分钟，期间能用但会慢。")
        }
        let counts = requestCounts([n])[n]
        if let c = counts {
            text += c.active + c.waiting > 0
                ? L("\n\n\(machine(n)) 上有 \(c.active) 个请求在算、\(c.waiting) 个在排队。切换会立刻中断它们（这个弹窗开着时新来的也一样）；想让它们答完，就点取消，过会儿再拨。")
                : L("\n\n\(machine(n)) 此刻没有请求在处理。这个弹窗开着时新来的请求，切换时也会被中断。")
        } else {
            text += L("\n\n读不到 \(machine(n)) 此刻的请求数；正在用它的对话会被中断。")
        }
        guard confirm(L("把 \(machine(n)) 上 \(meta.title) 的 \(label) 改为\(word)？"), text, ok: L("切换")) else { objectWillChange.send(); return }
        runOption(label, n, meta, args, free: false)
    }

    /// 切换开关；本机上 Ollama 占着内存、按新设置放不下时，先问一次再让它释放（同 runLoad）
    private func runOption(_ label: String, _ n: String, _ meta: ModelMeta, _ args: [String], free: Bool) {
        perform(L("切换 \(machine(n)) 上 \(meta.title) 的 \(label)"), args + ["--yes"] + (free ? ["--free-ollama"] : []), summary: true) { errors in
            guard errors.contains("--free-ollama") else { return }
            if self.confirm(L("让 Ollama 先释放内存？"), L("Ollama 正占着 \(self.localMachine) 的内存，按新设置加载 \(meta.title) 会超过上限。确认后会请 Ollama 卸载它当前的模型，再切换。"),
                            ok: L("释放并切换")) {
                self.runOption(label, n, meta, args, free: true)
            }
        }
    }

    func selftest(_ m: String, on n: String) {
        perform(L("自检 \(machine(n)) 的 \(title(m, on: n))"), ["selftest", m, "--on", n], summary: true)
    }

    func clearCache(_ n: String) {
        let size = gbText(node(n)?.cache_gb ?? 0)
        guard confirm(L("清空 \(machine(n)) 的多轮缓存？"),
                      L("会删除 \(machine(n)) 上的多轮缓存（约 \(size)）。删除后不可恢复；之后第一次问同一份资料会慢一些（要重新读入），模型本身不受影响。"),
                      ok: L("清空"), critical: true) else { return }
        perform(L("清空 \(machine(n)) 的缓存"), ["cache", "clear", n, "--yes"])
    }

    func refreshNow() { refresh(full: true) }

    func verifyMachine(_ name: String, onDone: ((Bool) -> Void)? = nil) {
        guard !Backend.isPreview, topState()?.enabled == true else { perform(L("验证 \(name)"), ["machine", "verify", name], summary: true, onDone: onDone); return }
        guard let proof = topProof(["chip", "payload", "state", "machine-add", name], intent: .addMachine, reason: L("把新机器加进机器表")),
              let secret = askSecret(L("添加机器"), L("新机器进了机器表，主机就能操作它。输入最高密码。"), ok: L("添加")) else { return }
        perform(L("添加 \(name)"), ["machine", "verify", name], summary: true, input: secret[0] + "\n" + proof + "\n")
    }

    func removeMachine(_ name: String, _ title: String) {
        let hubMode = !Backend.isPreview && topState()?.enabled == true
        guard hubMode else {
            guard confirm(L("从这台的控制台移除 \(title)？"), L("会删除连它的钥匙。它上面的模型和服务不受影响；它那边的授权还留着，要在 \(title) 的控制台吊销。"), ok: L("移除"), critical: true) else { return }
            perform(L("移除 \(title)"), ["machine", "remove", name, "--yes"], summary: true)
            return
        }
        let list = waitFor { try? JSONDecoder().decode(MachineList.self, from: Data(Backend.run(["machine", "list", "--json"]).out.utf8)) }
        if list?.pending.contains(where: { $0.name == name }) == true {
            guard confirm(L("取消添加 \(title)？"), L("它还没进机器表，取消后配对串作废。"), ok: L("取消添加")) else { return }
            perform(L("取消添加 \(title)"), ["machine", "remove", name, "--yes"], summary: true)
            return
        }
        guard confirm(L("把 \(title) 移出机器表？"), Backend.showsHub ? L("各台不再认它，它上面的模型和服务不受影响。它那边的授权和加密钥匙要到那台的 设置 → 安全 点退出主机模式才清掉。机器丢了或卖了，还要吊销它的安全芯片钥匙。")
                      : L("各台不再认它，它上面的模型和服务不受影响。它那边的授权还留着，要在 \(title) 的控制台吊销。"), ok: L("下一步"), critical: true),
              let proof = topProof(["chip", "payload", "state", "machine-remove", name], intent: .removeMachine(name), reason: L("把 \(title) 移出机器表")),
              let secret = askSecret(L("移除 \(title)"), L("输入最高密码。"), ok: L("移除")) else { return }
        perform(L("移除 \(title)"), ["machine", "remove", name, "--yes"], summary: true, input: secret[0] + "\n" + proof + "\n")
    }

    /// 要在系统弹出的对话框里亲手点"允许"（clusterctl 弹的，远程走不通这一步）
    func allowMachine(_ pair: String, level: String) { perform(L("允许新机器"), ["grants", "allow", pair, level], summary: true) }

    func revokeGrant(_ grant: GrantRow) {
        guard confirm(L("吊销「\(grant.title)」？"), L("它连这台的隧道和控制会立刻断开。别的控制方也会断一下，自动重连；正在流式输出的请求会中断。"), ok: L("吊销"), critical: true) else { return }
        perform(L("吊销「\(grant.title)」"), ["grants", "revoke", grant.id, "--yes"], summary: true)
    }
    func doctor() { perform(L("一键体检"), ["doctor"], showLog: true, summary: true) }

    func setContext(_ model: String, on node: String, _ value: Int) {
        if self.node(node)?.contexts?[model]?.over?.contains(value) == true {
            guard confirm(L("\(contextLabel(value)) 对话塞满时会超内存安全线"), L("这一档能选，平时也不多占内存；但对话一直聊到接近上限时，\(machine(node)) 的内存会超过安全线，可能开始拿硬盘顶内存，模型变得极慢。"),
                          ok: L("仍然改成 \(contextLabel(value))")) else { return }
        }
        perform(L("设置上下文"), ["context", node, model, String(value)], showLog: false, summary: true)
    }

    /// 多轮缓存（按机器）：value 0 是关，否则是上限 GB。不用重启服务，但这台的模型要卸载、改完再加载回来；服务没开时只存。
    /// value：0 关，正数是上限（GB），-1 是官方版的"自动"（A14）
    func setCache(_ value: Int, on n: String) {
        guard let info = node(n) else { return }
        let auto = value < 0
        let word = auto ? L("自动") : value == 0 ? L("关") : "\(value) GB", arg = auto ? "auto" : value == 0 ? "off" : String(value)
        guard info.up else {
            perform(L("设置 \(machine(n)) 的多轮缓存"), ["cache", "set", n, arg], showLog: false, summary: true)
            return
        }
        let names = info.active.map { title($0) }.joined(separator: L("、"))
        let used = info.cache_gb ?? 0, shrink = value > 0 && used > Double(value) * 0.9
        var text = L("不用重启服务。")
        if !names.isEmpty {
            let eta = info.active.compactMap { model($0)?.eta }.joined(separator: L("、"))
            text = L("不用重启服务，但 \(machine(n)) 上的 \(names) 会先卸载、改完再加载回来（下线\(eta)）。")
            if let c = requestCounts([n])[n] {
                text += c.active + c.waiting > 0 ? L("\n\n此刻有 \(c.active) 个请求在算、\(c.waiting) 个在排队，会被立刻中断；想让它们答完，就点取消，过会儿再改。")
                                                 : L("\n\n此刻没有请求在处理。")
            } else {
                text += L("\n\n读不到此刻的请求数；正在用它的对话会被中断。")
            }
        }
        if shrink {
            text += L("\n\n现在占 \(gbText(used))，比新上限大：\(names.isEmpty ? L("下次") : "")加载时会删掉最久没用的笔记，删到约 \(Int(Double(value) * 0.9)) GB，不能恢复。这些内容下次问要重新读。")
        }
        if value == 0 {
            text += L("\n\n关掉后资料不再写入这台的硬盘，同一份资料再问要从头读。已有的缓存（\(gbText(used))）不会自动删，可以点「清空缓存」。")
        }
        if auto {
            text += L("\n\n「自动」是 oMLX 默认的上限：按（剩余硬盘 + 已有缓存）÷ 2 定，随硬盘剩多少变。")
        }
        guard confirm(L("把 \(machine(n)) 的多轮缓存改成 \(word)？"), text, ok: L("改成 \(word)"), critical: shrink) else { return }
        runCache(n, word, arg, free: false)
    }

    private func runCache(_ n: String, _ word: String, _ arg: String, free: Bool) {
        perform(L("把 \(machine(n)) 的多轮缓存改成 \(word)"), ["cache", "set", n, arg, "--yes"] + (free ? ["--free-ollama"] : []), summary: true) { errors in
            guard errors.contains("--free-ollama") else { return }
            if self.confirm(L("让 Ollama 先释放内存？"), L("Ollama 正占着 \(self.localMachine) 的内存，模型加载回来时会超过上限。确认后会请 Ollama 卸载它当前的模型，再改。"),
                            ok: L("释放并改")) {
                self.runCache(n, word, arg, free: true)
            }
        }
    }

    func copyMainKey() { perform(L("复制主密钥"), ["key", "copy"], showLog: false) }
    func copySubKey(_ name: String) { perform(L("复制子密钥「\(name)」"), ["subkey", "copy", name], showLog: false) }
    func createSubKey(_ name: String) { perform(L("新建子密钥「\(name)」"), ["subkey", Backend.isManager ? "create-print" : "create", name], showLog: false) }

    // MARK: 主机角色

    @Published var hub: HubView?
    @Published var hubConflicts: [String] = []
    @Published var hubUnjoined: [String] = []
    @Published var chip: ChipView?
    @Published var chipKeyId: String?
    @Published var chipLost = false
    private var lastHubCheck = Date.distantPast
    private var hubChecking = false
    private var hubAgain = false

    /// 主机面板打开时和每分钟查一次：谁在申请。这要问一圈各台，不放进 10 秒一次的刷新。
    /// 上一次还没回来就不再发，只记下，回来后补查一次（以前每分钟照发，堵住时一分钟叠一个，踩坑 #63）
    func checkHub() {
        guard !hubChecking else { hubAgain = true; return }
        hubChecking = true
        lastHubCheck = Date()
        DispatchQueue.global().async {
            let decoded = try? JSONDecoder().decode(HubView.self, from: Data(Backend.run(["hub", "view"]).out.utf8))
            struct Synced: Decodable { let conflicts: [String]?; let unjoined: [String]? }
            var synced: Synced?
            if decoded?.is_hub == true && !Backend.isManager {   // 改密码、改名时没连上的机器，连上后补发
                synced = try? JSONDecoder().decode(Synced.self, from: Data(Backend.run(["hub", "sync"]).out.utf8))
            }
            DispatchQueue.main.async {
                self.hubConflicts = synced?.conflicts ?? []
                self.hubUnjoined = synced?.unjoined ?? []
                let before = self.hub?.applications ?? []
                self.hub = decoded ?? self.hub
                if let now = decoded?.applications, !now.isEmpty, now != before, self.bounceDock { NSApp.requestUserAttention(.criticalRequest) }
                self.hubChecking = false
                if self.hubAgain { self.hubAgain = false; self.checkHub() }
            }
        }
    }

    func hubTitle(_ name: String?) -> String { name.flatMap { hub?.machines?[$0] } ?? name ?? "" }

    /// 问最高密码：安全输入框，经标准输入交给 clusterctl，不进命令行和日志
    func askSecret(_ title: String, _ text: String, ok: String, fields: [String] = [L("最高密码")]) -> [String]? {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = .warning
        let inputs = fields.map { name -> NSSecureTextField in
            let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
            field.placeholderString = name
            return field
        }
        let stack = NSStackView(views: inputs)
        stack.orientation = .vertical
        stack.spacing = 8
        stack.frame = NSRect(x: 0, y: 0, width: 300, height: CGFloat(fields.count) * 32)
        alert.accessoryView = stack
        alert.window.initialFirstResponder = inputs.first
        alert.addButton(withTitle: ok)
        alert.addButton(withTitle: L("取消"))
        guard Console.present(alert) == .alertFirstButtonReturn else { return nil }
        return inputs.map { $0.stringValue }
    }

    private func hubDone(_ ok: Bool) { checkHub(); refresh(full: true) }

    /// 人在机器前的确认：指纹或 Mac 密码，系统自己弹框。不用提前设置，防的是手滑，不防同账号的程序
    func verifyOwner(_ reason: String) -> Bool {
        if Backend.isPreview { return true }
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
            _ = confirm(L("验证不了这台 Mac 的主人"), error?.localizedDescription ?? L("系统没有给出原因。"), ok: L("好"))
            return false
        }
        var done = false, passed = false
        context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, _ in
            DispatchQueue.main.async { passed = ok; done = true }
        }
        while !done { RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05)) }
        return passed
    }

    // MARK: 安全芯片确认（用户 11-04 定）：确认 → 安全芯片签名（系统弹框）→ 最高密码。签名由被操作的机器按自己的登记表核对

    /// 等后台做完，界面不卡；系统弹框和连别的机器都可能要一会儿
    private func waitFor<T>(_ work: @escaping () -> T) -> T {
        var result: T?
        DispatchQueue.global().async {
            let value = work()
            DispatchQueue.main.async { result = value }
        }
        while result == nil { RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05)) }
        return result!
    }

    /// 那几台此刻的请求数，给确认框用。各台同时问、在后台问，这里转着事件循环等，最多 5 秒；没问到的不在结果里，
    /// 确认框按"读不到"说。以前在主线程上逐台干等（远程每台最长 8 秒，全部关机、退出时逐台累加）：oMLX 卡住或网络半通时，
    /// 点卸载、关闭、关机、⌘Q 整个界面转彩球（体检 A8）。结果到带锁的盒子里取，不靠主队列送回来，
    /// 所以从 main.async 的块里调、在退出流程里调都不会等死（waitFor 在那种地方会）
    private var askingCounts = false

    private func requestCounts(_ keys: [String]) -> [String: RequestCounts] {
        guard !keys.isEmpty, !askingCounts else { return [:] }
        askingCounts = true
        defer { askingCounts = false }
        let found = Locked((counts: [String: RequestCounts](), left: keys.count))
        for key in keys {
            Thread {
                let c = try? JSONDecoder().decode(RequestCounts.self, from: Data(Backend.run(["requests", key]).out.utf8))
                found.with { if let c = c { $0.counts[key] = c }; $0.left -= 1 }
            }.start()
        }
        let deadline = Date().addingTimeInterval(5)
        while found.get().left > 0 && Date() < deadline { RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05)) }
        return found.get().counts
    }

    private func notify(_ title: String, _ text: String) {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.addButton(withTitle: L("好"))
        _ = Console.present(alert)
    }

    /// 后端说的失败原因。rpc 放在 @@error，本机的在 stderr
    private func failure(_ r: (code: Int32, out: String, err: String)) -> String {
        for line in r.out.split(separator: "\n") where line.hasPrefix("@@error ") {
            if let text = try? JSONDecoder().decode(String.self, from: Data(line.dropFirst(8).utf8)) { return text }
        }
        let text = r.err.replacingOccurrences(of: "✕ ", with: "").replacingOccurrences(of: L("操作未完成："), with: "").trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? L("没有完成（退出码 \(r.code)）。") : text
    }

    private func signText(_ r: (code: Int32, out: String, err: String)) -> String? {
        struct Reply: Decodable { let text: String }
        guard r.code == 0, let reply = try? JSONDecoder().decode(Reply.self, from: Data(r.out.utf8)) else {
            notify(L("没有做"), failure(r))
            return nil
        }
        return reply.text
    }

    private func loadChip() -> ChipView? { try? JSONDecoder().decode(ChipView.self, from: Data(Backend.run(["chip", "view"]).out.utf8)) }

    /// 设置页打开时查一次。主机要问一圈各台有没有人申请，不放进 10 秒一次的刷新
    func checkChip() {
        DispatchQueue.global().async {
            let view = self.loadChip()
            let mine = Backend.isPreview ? view?.keys.first?.id : Chip.publicKey().map(Chip.keyId)
            let lost = !Backend.isPreview && Chip.keyLost
            DispatchQueue.main.async { self.chip = view; self.chipKeyId = mine; self.chipLost = lost }
        }
    }

    private func machineTitle() -> String {
        let name = waitFor { Host.current().localizedName ?? L("这台 Mac") }
        return String(name.filter { !"\"\\".contains($0) && !$0.isNewline }.prefix(64))
    }

    private func signed(_ text: String, reason: String) -> String? {
        NSApp.activate(ignoringOtherApps: true)
        let result: Result<String, Error> = waitFor { Result { try Chip.sign(text, reason: reason) } }
        switch result {
        case .success(let value): return value
        case .failure(let error): notify(L("安全芯片没有签名"), error.localizedDescription); return nil
        }
    }

    private func oneLine(_ value: [String: Any]) -> String {
        String(decoding: (try? JSONSerialization.data(withJSONObject: value)) ?? Data(), as: UTF8.self)
    }

    /// 这台登记了没有。没有就说清楚怎么办，返回假。allowPasswordOnly：主机开了"允许只用最高密码"时也放行
    private func chipReady(allowPasswordOnly: Bool = true) -> (ok: Bool, passwordOnly: Bool) {
        guard let view = waitFor({ self.loadChip() }) else {
            notify(L("读不到安全芯片的登记"), L("连不上主机，或者这台的登记文件有问题。什么都没动。"))
            return (false, false)
        }
        chip = view
        let mine = Chip.publicKey().map(Chip.keyId)
        chipKeyId = mine
        if let mine = mine, view.keys.contains(where: { $0.id == mine }) { return (true, false) }
        if view.password_only && allowPasswordOnly { return (true, true) }
        if !Chip.isAvailable {
            notify(L("这台 Mac 没有安全芯片"), L("这个操作要到已登记的机器上做。"))
        } else if view.enabled {
            notify(L("这台还没有登记安全芯片"), L("最高操作要本人确认。点右上角的设置 → 安全 → 申请登记，到已登记的机器上批准；或者用恢复码登记。"))
        } else {
            notify(L("先登记安全芯片"), L("最高操作要本人确认（指纹或 Mac 密码）。点右上角的设置 → 安全，登记这台。"))
        }
        return (false, false)
    }

    private func matches(_ text: String, _ intent: TopIntent, _ view: TopState?) -> Bool {
        Chip.matches(text, intent, TopContext(enabled: view?.enabled == true, term: view?.term ?? 0, hub: view?.hub, candidates: view?.candidates ?? [], local: localKey))
    }

    private func topState() -> TopState? { waitFor { try? JSONDecoder().decode(TopState.self, from: Data(Backend.run(["hub", "state"]).out.utf8)) } }

    private func bodyMatches(_ text: String, keys: [String], recovery: String? = nil, passwordOnly: Bool? = nil) -> Bool {
        Chip.bodyMatches(text, seq: chip?.seq ?? 0, keys: keys, recovery: recovery, recoveryId: chip?.recovery_id, passwordOnly: passwordOnly ?? chip?.password_only ?? false)
    }

    private func refuseTampered() {
        notify(L("内容对不上，没有签"), L("后端要签的内容和你点的操作不一样，什么都没动。程序可能被改过，到别的机器上查一下，先不要继续做最高操作。"))
    }

    /// 最高操作的确认。默认（没登记安全芯片）只要系统弹框，指纹或 Mac 密码，不用提前设置；
    /// 登记了才向后端要内容、自己核对、用安全芯片签名。needsPassword：主机模式的操作必须有最高密码，主机钥匙靠它加密；关机不强制。
    /// 返回一行，和最高密码一起经标准输入传。nil 是没确认，调用方什么都不做
    func topProof(_ args: [String], intent: TopIntent, reason: String, needsPassword: Bool = true) -> String? {
        if Backend.isPreview { return "{}" }
        guard running == nil else { notify(L("有操作正在进行"), L("等它做完再试。")); return nil }
        let view = topState()
        if needsPassword && view?.password_set == false {
            notify(L("先设最高密码"), Backend.isManager ? L("最高密码要在主机本机设：到主机的 设置 → 安全。") : L("点右上角的设置 → 安全，设好最高密码再做。"))
            return nil
        }
        guard let registry = waitFor({ self.loadChip() }) else {
            notify(L("读不到安全芯片的登记"), L("连不上主机，或者这台的登记文件有问题。什么都没动。"))
            return nil
        }
        chip = registry
        if !registry.enabled {
            guard verifyOwner(reason) else { return nil }
            return needsPassword ? "" : "owner-confirmed"
        }
        let ready = chipReady()
        guard ready.ok else { return nil }
        if ready.passwordOnly { return "" }
        guard let id = chipKeyId, let text = signText(waitFor { Backend.run(args) }) else { return nil }
        guard matches(text, intent, view) else { refuseTampered(); return nil }
        guard let signature = signed(text, reason: reason) else { return nil }
        return oneLine(["payload": text, "key": id, "sig": signature])
    }

    /// 关机的最高密码是可选的：设了才问，没设传空行。读不到状态时当设了，错了后端会拒
    private func passwordIfSet(_ title: String) -> String? {
        if topState()?.password_set == false { return "" }
        return askSecret(title, L("输入最高密码，才会真的关机。"), ok: L("关机"))?[0]
    }

    /// 恢复码只显示这一次，不写文件。可以复制，30 秒后清掉剪贴板
    private func showRecovery(_ code: String) -> Bool {
        while true {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = L("恢复码（只显示这一次）")
            alert.informativeText = L("登记的机器都没了时，靠它登记新钥匙。存进密码管理器或打印出来。\n\n丢了也不影响日常使用，只是到时候要到每台机器前重新配对。")
            alert.alertStyle = .warning
            let label = NSTextField(labelWithString: code)
            label.font = .monospacedSystemFont(ofSize: 15, weight: .medium)
            label.isSelectable = true
            label.sizeToFit()
            alert.accessoryView = label
            alert.addButton(withTitle: L("我存好了"))
            alert.addButton(withTitle: L("复制"))
            alert.addButton(withTitle: L("取消"))
            switch Console.present(alert) {
            case .alertFirstButtonReturn: return true
            case .alertSecondButtonReturn: Backend.copySecret(code)
            default: return false
            }
        }
    }

    private func chipBody(_ action: String, _ form: [String: Any]) -> String? {
        let input = oneLine(form)
        return signText(waitFor { Backend.run(["chip", "body", action], input: input) })
    }

    /// 交回后端：逐条核对后存下，主机再发给各台
    private func chipSubmit(_ done: String, body text: String, signer: String, signature: String) {
        let input = oneLine(["updates": [["body": text, "signer": signer, "sig": signature]]])
        let r = waitFor { Backend.run(["chip", "apply"], input: input) }
        defer { checkChip() }
        guard r.code == 0 else { notify(L("没有成功"), failure(r)); return }
        struct Reply: Decodable { let missed: [String]? }
        // 副机发不到别的机器：只改了这台，等主机来拿
        guard let missed = (try? JSONDecoder().decode(Reply.self, from: Data(r.out.utf8)))?.missed else {
            notify(done, L("这台改好了。别的机器还不知道：打开主机的 设置 → 安全，主机会拿走并发给各台。"))
            return
        }
        notify(done, missed.isEmpty ? L("各台都收到了。") : L("这些机器这次没收到，连上后补发：\(missed.joined(separator: L("、")))。"))
    }

    /// 这台的钥匙，没有才建。读不出来时先问：可能只是一时读不到，换了就要重新登记
    private func myKey() throws -> String? {
        guard !Chip.screenLocked else { throw ChipError.locked }
        if let pub = Chip.publicKey() { return pub }
        if Chip.hasKey(), !confirm(L("这台的钥匙读不出来"), L("抹过盘、换过主板会这样。确定要换一把新的吗？旧的钥匙包留着，不删。"), ok: L("换一把新的"), critical: true) { return nil }
        return try Chip.createKey()
    }

    private func refreshChip() { chip = waitFor { self.loadChip() } ?? chip }

    /// 第一把钥匙。主机上就是这条路；副机只有主机没了才走（chipStartNew）。先签名再显示恢复码：
    /// 不然用户存了新码、又取消了指纹，手里的码就对不上了
    func chipRegisterFirst() {
        guard !Backend.isPreview else { return }
        guard confirm(L("登记这台 Mac 的安全芯片？"), L("以后关机、移交、接管、改名单、改最高密码，系统都会弹框要指纹或 Mac 密码。\n\n系统先弹一次框确认登记，然后显示恢复码，只显示一次，请准备好密码管理器。"), ok: L("下一步")) else { return }
        do {
            refreshChip()
            guard chip?.enabled != true, let pub = try myKey() else { return }
            let code = Chip.newRecoveryCode(), recovery = try Chip.recoveryPublic(code)
            guard let text = chipBody("init", ["title": machineTitle(), "pub": pub, "recovery": recovery]) else { return }
            guard bodyMatches(text, keys: [Chip.keyId(pub)], recovery: recovery, passwordOnly: false) else { refuseTampered(); return }
            guard let signature = signed(text, reason: L("登记这台 Mac 的安全芯片钥匙")), showRecovery(code) else { return }
            chipSubmit(L("登记好了"), body: text, signer: Chip.keyId(pub), signature: signature)
        } catch { notify(L("没有登记"), error.localizedDescription) }
    }

    /// 副机开始一份新的登记：主机还在时不该走这条路，那样两边接不上
    func chipStartNew() {
        guard confirm(L("在这台开始新的登记？"), L("只有主机没了、别的登记机器也没了、恢复码也丢了才用。主机还在的话，应该点申请登记，回主机批准；不然两边接不上，要到每台清掉重来。"), ok: L("我明白，继续"), critical: true) else { return }
        chipRegisterFirst()
    }

    /// 别的机器发来的第一份登记：人在这台机器前核对过指纹才生效
    func chipAccept() {
        guard !Backend.isPreview, let pending = chip?.pending else { return }
        guard confirm(L("接受这份登记？"), L("第一把钥匙：\(pending.title)，登记表指纹 \(pending.root)，共 \(pending.seq) 条。\n\n到登记第一把钥匙的那台（通常是主机），看 设置 → 安全 里这台 Mac 的指纹和条数，要一模一样。不是你刚登记的就别接受：接受后，这把钥匙就能关这台。"), ok: L("接受")),
              verifyOwner(L("接受安全芯片登记")) else { return }
        let r = waitFor { Backend.run(["chip", "accept", pending.digest]) }
        if r.code != 0 { notify(L("没有接受"), failure(r)) }
        checkChip()
    }

    /// 别的机器：写一条申请，到已登记的机器上批准
    func chipRequest() {
        guard !Backend.isPreview else { return }
        do {
            guard let pub = try myKey() else { return }
            let input = oneLine(["title": machineTitle(), "pub": pub])
            let r = waitFor { Backend.run(["chip", "request"], input: input) }
            guard r.code == 0 else { notify(L("没有申请成"), failure(r)); return }
            notify(L("已申请"), L("到已登记的机器（通常是主机）的 设置 → 安全 批准，核对指纹是不是 \(Chip.fingerprint(publicText: pub))。10 分钟内有效。"))
        } catch { notify(L("没有申请成"), error.localizedDescription) }
        checkChip()
    }

    func chipApprove(_ request: ChipView.Request) {
        guard !Backend.isPreview, !Backend.isManager, let id = chipKeyId else { return }
        refreshChip()
        // 指纹按公钥当场算，不信后端写的
        let print = Chip.fingerprint(publicText: request.pub), keys = (chip?.keys.map(\.id) ?? []) + [Chip.keyId(request.pub)]
        guard confirm(L("批准 \(request.title) 登记？"), L("来自 \(request.where ?? L("别的机器"))。指纹 \(print)，到那台的 设置 → 安全 看一眼，要一模一样。不是你刚点的就别批。"), ok: L("批准")),
              let text = chipBody("add", ["title": request.title, "pub": request.pub]) else { return }
        guard bodyMatches(text, keys: keys) else { refuseTampered(); return }
        guard let signature = signed(text, reason: L("批准 \(request.title) 登记安全芯片钥匙")) else { return }
        chipSubmit(L("批准了"), body: text, signer: id, signature: signature)
    }

    func chipRevoke(_ key: ChipView.Key) {
        guard !Backend.isPreview, !Backend.isManager, let id = chipKeyId else { return }
        refreshChip()
        let mine = key.id == id, keys = (chip?.keys.map(\.id) ?? []).filter { $0 != key.id }
        guard confirm(L("吊销 \(key.title) 的钥匙？"), (mine ? L("这是这台自己的钥匙。") : "") + L("吊销后那台做不了最高操作，要重新登记。机器丢了、卖了就吊销。"), ok: L("吊销"), critical: true),
              let text = chipBody("revoke", ["id": key.id]) else { return }
        guard bodyMatches(text, keys: keys) else { refuseTampered(); return }
        guard let signature = signed(text, reason: L("吊销 \(key.title) 的安全芯片钥匙")) else { return }
        chipSubmit(L("吊销了"), body: text, signer: id, signature: signature)
    }

    func chipNewRecovery() {
        guard !Backend.isPreview, !Backend.isManager, let id = chipKeyId else { return }
        guard confirm(L("换一个恢复码？"), L("系统先弹框确认，然后显示新的恢复码。确认保存后，旧的才失效。"), ok: L("下一步")) else { return }
        do {
            refreshChip()
            let code = Chip.newRecoveryCode(), recovery = try Chip.recoveryPublic(code)
            guard let text = chipBody("recovery", ["recovery": recovery]) else { return }
            guard bodyMatches(text, keys: chip?.keys.map(\.id) ?? [], recovery: recovery) else { refuseTampered(); return }
            guard let signature = signed(text, reason: L("换一个恢复码")), showRecovery(code) else { return }
            chipSubmit(L("换好了"), body: text, signer: id, signature: signature)
        } catch { notify(L("没有换"), error.localizedDescription) }
    }

    /// 登记的机器都没了：恢复码只能登记一把新钥匙，用一次就换新的。先核对码对不对，再让用户存新码
    func chipRecover() {
        guard !Backend.isPreview else { return }
        guard let typed = askSecret(L("用恢复码登记这台"), L("恢复码只能用来登记一把新钥匙，用过就作废，会生成一个新的。之后记得吊销丢了的那台。"), ok: L("下一步"), fields: [L("恢复码（24 位）")]) else { return }
        do {
            refreshChip()
            guard Chip.keyId(try Chip.recoveryPublic(typed[0])) == chip?.recovery_id else { notify(L("恢复码不对"), L("和登记表里的对不上，什么都没动。可能是旧的恢复码，或者抄错了。")); return }
            guard let pub = try myKey() else { return }
            let code = Chip.newRecoveryCode(), recovery = try Chip.recoveryPublic(code)
            guard let text = chipBody("recover", ["title": machineTitle(), "pub": pub, "recovery": recovery]) else { return }
            guard bodyMatches(text, keys: (chip?.keys.map(\.id) ?? []) + [Chip.keyId(pub)], recovery: recovery) else { refuseTampered(); return }
            guard showRecovery(code) else { return }
            chipSubmit(L("登记好了，记得吊销丢了的那台"), body: text, signer: "recovery", signature: try Chip.signWithRecovery(text, code: typed[0]))
        } catch { notify(L("没有登记"), error.localizedDescription) }
    }

    /// 恢复码也丢了：到每台机器前，过 macOS 管理员验证，清掉这台的登记，重新来。模型和数据不受影响
    func chipForget() {
        guard !Backend.isPreview else { return }
        guard confirm(L("清掉这台的安全芯片登记？"), L("只有登记弄乱了、恢复码也丢了才用。这台的钥匙和登记表都清掉。之后别的机器发来的登记不会自动生效，要你在这里核对指纹后接受。模型和数据不受影响。"), ok: L("下一步"), critical: true),
              let nonce = adminProof(L("清掉安全芯片登记要 macOS 管理员验证。")) else { return }
        let r = waitFor { Backend.run(["chip", "forget", nonce]) }
        guard r.code == 0 else { notify(L("没有清掉"), failure(r)); return }
        Chip.retireKey()
        notify(L("清掉了"), L("主机还在：到主机上重新登记，再回这里接受。主机没了：在这里点开始新的登记。"))
        checkChip()
    }

    /// 重新配对的最后一步：主机也没了、最高密码也丢了，到每台退出主机模式，再从头启用
    func hubLeave() {
        guard !Backend.isPreview else { return }
        guard confirm(L("退出主机模式？"), L("这台不再认现在的主机，加密的钥匙和主机钥匙的授权都删掉。模型和服务不受影响。之后要重新启用主机模式，并到每台重新贴授权行。主机还在时不要用。"), ok: L("下一步"), critical: true),
              let nonce = adminProof(L("退出主机模式要 macOS 管理员验证。")) else { return }
        perform(L("退出主机模式"), ["hub", "leave", nonce], summary: true, onDone: hubDone)
    }

    /// 换过钥匙、吊销过钥匙后，旧签名在别的机器上过不了：重新签一次，各台就都跟上了
    func hubResign() {
        guard confirm(L("重新签一次？"), L("主机、名单都不变，只是用现在登记的钥匙再签一次，让各台跟上。"), ok: L("下一步")),
              let proof = topProof(["chip", "payload", "state", "resign"], intent: .password, reason: L("重新签一次主机状态")),
              let secret = askSecret(L("重新签一次"), L("输入最高密码。"), ok: L("签")) else { return }
        perform(L("重新签一次"), ["hub", "resign"], summary: true, input: secret[0] + "\n" + proof + "\n", onDone: hubDone)
    }

    /// 带安全芯片确认的改密码：先验旧密码、封好新文件，再签名，签完才真的换
    private func changePassword(prepare: [String], input: String, reason: String) {
        let view = topState()
        guard let text = signText(waitFor { Backend.run(prepare, input: input) }), let id = chipKeyId else { return }
        guard matches(text, .password, view) else { refuseTampered(); return }
        guard let signature = signed(text, reason: reason) else { return }
        perform(reason, ["hub", "password", "commit"], summary: true, input: oneLine(["payload": text, "key": id, "sig": signature]) + "\n", onDone: hubDone)
    }

    /// 第一次设最高密码（用户 11-04 定：在本机的设置里设）。可以早于启用主机模式
    func setPassword() {
        guard verifyOwner(L("设置ModelTower的最高密码")),
              let secret = askSecret(L("设最高密码"), L("至少 8 位，不要全是数字。这里只存校验值，不存明文，忘了可以重设。"),
                                     ok: L("设好"), fields: [L("最高密码"), L("再输一次")]) else { return }
        guard secret[0] == secret[1] else { _ = confirm(L("两次不一样"), L("没有设置。"), ok: L("好")); return }
        perform(L("设最高密码"), ["hub", "password", "set"], input: secret[0] + "\n", onDone: hubDone)
    }

    /// 忘了最高密码：过 macOS 管理员密码框，留下只有 root 能写的凭证，再设新的。主机钥匙在这台，所以不用旧密码
    func forgotPassword() {
        guard confirm(L("忘记最高密码？"), L("这台是主机，主机钥匙就在这里，可以直接设一个新的。系统会先要 macOS 管理员验证。新密码会发给名单里的机器，连得上的立刻换新；没连上的那台连上后补发。"), ok: L("下一步")),
              let nonce = adminProof() else { return }
        guard let secret = askSecret(L("设新的最高密码"), L("至少 8 位，不要全是数字。"), ok: L("设好"), fields: [L("新的最高密码"), L("再输一次")]) else { return }
        guard secret[0] == secret[1] else { _ = confirm(L("两次不一样"), L("没有改。"), ok: L("好")); return }
        if let view = waitFor({ self.loadChip() }), view.enabled, !view.password_only {
            guard chipReady(allowPasswordOnly: false).ok else { return }
            changePassword(prepare: ["hub", "password", "prepare-reset", nonce], input: secret[0] + "\n", reason: L("重设最高密码"))
            return
        }
        perform(L("重设最高密码"), ["hub", "password", "reset", nonce], summary: true, input: secret[0] + "\n", onDone: hubDone)
    }

    /// 系统的管理员密码框：Agent 过不了这个框
    private func adminProof(_ prompt: String = L("重设最高密码要 macOS 管理员验证。")) -> String? {
        let nonce = (0..<16).map { _ in String(format: "%02x", UInt8.random(in: 0...255)) }.joined()
        if Backend.isPreview { return nonce }
        let script = "do shell script \"/usr/bin/touch /var/run/lmc-reset-\(nonce)\" with prompt \"\(prompt)\" with administrator privileges"
        return runAdmin(script) ? nonce : nil
    }

    func runAdmin(_ script: String) -> Bool {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        while p.isRunning { RunLoop.current.run(mode: .default, before: Date(timeIntervalSinceNow: 0.05)) }
        return p.terminationStatus == 0
    }

    /// 新机器的"一次设好"：断电自动开机、防休眠、允许控制台关它。用户本人在系统密码框里确认
    func applyPrepare(_ command: String) -> Bool {
        if Backend.isPreview { return true }
        return runAdmin(L("do shell script \"\(command)\" with prompt \"让这台 Mac 准备好当模型机：断电后自动开机、不自动休眠、允许控制台关机。\" with administrator privileges"))
    }

    func hubHandover(to target: String, approving: Bool = false) {
        let name = hubTitle(target)
        guard confirm(approving ? L("批准 \(name) 当主机？") : L("把主机移交给 \(name)？"),
                      L("模型和缓存都留在原机器，只有入口换地方。正在经入口的请求答完才切换，这台之后变成副机。\n\n可当主机的名单：\((hub?.candidates ?? []).map(hubTitle).joined(separator: L("、")))。多了不认识的机器就别继续。"), ok: L("下一步")),
              let proof = topProof(["chip", "payload", "state", "handover", target], intent: .handover(target), reason: L("把主机移交给 \(name)")),
              let secret = askSecret(L("确认移交"), L("输入最高密码，才会真的移交给 \(name)。"), ok: L("移交")) else { return }
        perform(L("把主机移交给 \(name)"), ["hub", approving ? "approve" : "handover", target], input: secret[0] + "\n" + proof + "\n", onDone: hubDone)
    }

    func hubReject(_ target: String) { perform(L("拒绝 \(hubTitle(target)) 的申请"), ["hub", "reject", target], onDone: hubDone) }
    func hubApply() { perform(L("申请当主机"), ["hub", "apply"], onDone: hubDone) }

    func hubTakeover() {
        let finish: (Bool) -> Void = { [weak self] ok in Backend.holdAlias = false; self?.hubDone(ok) }
        guard confirm(L("强制接管主机？"), L("主机崩了才用这个。这台会变成主机，原主机修好回来后自动退成副机。主机还连得上时会再问一次。"), ok: L("下一步"), critical: true),
              let proof = topProof(["chip", "payload", "state", "takeover"], intent: .takeover(Backend.isManager ? nil : localKey), reason: L("强制接管主机")),
              let secret = askSecret(L("强制接管"), L("输入最高密码。"), ok: L("接管")) else { Backend.holdAlias = false; return }
        perform(L("强制接管主机"), ["hub", "takeover"], input: secret[0] + "\n" + proof + "\n", onError: { [weak self] text in
            guard let self = self, text.contains(L("还连得上")),
                  self.confirm(L("主机还连得上"), L("正常情况用申请或移交。确实要强制接管吗？旧主机的指令会立刻失效。"), ok: L("强制接管"), critical: true) else { Backend.holdAlias = false; return }
            DispatchQueue.main.async { self.perform(L("强制接管主机"), ["hub", "takeover", "--force"], input: secret[0] + "\n" + proof + "\n", onDone: finish) }
        }, onDone: finish)
    }

    func hubEnable(candidates: [String]) {
        guard let proof = topProof(["chip", "payload", "state", "init"] + candidates, intent: .enable(candidates), reason: L("启用主机模式")),
              let secret = askSecret(L("启用主机模式"), L("输入最高密码。"), ok: L("启用")) else { return }
        perform(L("启用主机模式"), ["hub", "init"] + candidates, input: secret[0] + "\n" + proof + "\n", onDone: hubDone)
    }

    func hubJoin() { perform(L("通知各台"), ["hub", "join"], summary: true, onDone: hubDone) }

    // MARK: 管理版自己连得上哪些机器（连接时写进 ~/.ssh/lmc/config）

    struct KnownHost: Identifiable {
        let name: String
        var ip = "", user = "", port = "22"
        var id: String { name }
    }

    var knownHosts: [KnownHost] {
        var rows: [KnownHost] = []
        for raw in ((try? String(contentsOfFile: Backend.sshConfig, encoding: .utf8)) ?? "").split(separator: "\n") {
            let words = raw.split(separator: " ").map(String.init)
            guard words.count == 2 else { continue }
            if words[0] == "Host", words[1].hasPrefix("lmc-") { rows.append(KnownHost(name: String(words[1].dropFirst(4)))) }
            else if !rows.isEmpty {
                if words[0] == "HostName" { rows[rows.count - 1].ip = words[1] }
                if words[0] == "User" { rows[rows.count - 1].user = words[1] }
                if words[0] == "Port" { rows[rows.count - 1].port = words[1] }
            }
        }
        return rows
    }

    /// 在这台 Mac 上打开终端，直接连那台的苹果 SSH 解锁。密码由人输给系统，不经过这个应用
    func unlockDirect(_ host: KnownHost) {
        let dir = NSHomeDirectory() + "/.ssh/lmc"
        // 指纹记在这里；没开主机模式时还没有这个文件夹，ssh 存不下指纹（B3）。新建的文件夹只给自己读写
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let script = NSTemporaryDirectory() + "unlock-\(host.name).command"
        let line = "ssh -F none -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile='\(dir)/preboot_known_hosts' -p \(host.port) '\(host.user)@\(host.ip)'"
        try? L("#!/bin/zsh\necho '\(machine(host.name)) 开机解锁：下面输入这台的 macOS 密码，密码直接交给系统。看到 System successfully unlocked 就可以关掉这个窗口。'\n\(line)\n").write(toFile: script, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: script)
        NSWorkspace.shared.open(URL(fileURLWithPath: script))
    }

    /// 主机崩了：改连名单里的另一台，让它凭最高密码接管
    func takeover(on host: KnownHost) {
        Backend.holdAlias = true
        Backend.hubAlias = "lmc-" + host.name
        hubTakeover()
    }

    func hubChangePassword() {
        guard let secret = askSecret(L("改最高密码"), L("只能在主机本机改，改完会把新的加密钥匙发给名单里的机器。"), ok: L("改"), fields: [L("现在的最高密码"), L("新的最高密码"), L("再输一次")]) else { return }
        guard secret[1] == secret[2] else { _ = confirm(L("两次不一样"), L("没有改。"), ok: L("好")); return }
        if let view = waitFor({ self.loadChip() }), view.enabled, !view.password_only {
            guard chipReady(allowPasswordOnly: false).ok else { return }
            changePassword(prepare: ["hub", "password", "prepare"], input: secret[0] + "\n" + secret[1] + "\n", reason: L("修改最高密码"))
            return
        }
        guard verifyOwner(L("修改最高密码")) else { return }
        perform(L("改最高密码"), ["hub", "password"], summary: true, input: secret[0] + "\n" + secret[1] + "\n", onDone: hubDone)
    }

    func hubSetCandidates(_ names: [String]) {
        guard let proof = topProof(["chip", "payload", "state", "candidates"] + names, intent: .candidates(names), reason: L("修改可当主机的名单")),
              let secret = askSecret(L("改可当主机的名单"), L("只有名单里的机器存着加密的主机钥匙，才能被移交或强制接管。"), ok: L("改")) else { return }
        perform(L("改可当主机的名单"), ["hub", "candidates"] + names, summary: true, input: secret[0] + "\n" + proof + "\n", onDone: hubDone)
    }

    /// A11（已定第 31 条 ⑩）：把已有的模型文件夹加成 oMLX 模型目录的后一行。第一行不动，不往文件夹里写；oMLX 改目录时会卸载全部模型，所以有模型在线先确认
    func addModelsFolder() {
        guard running == nil, let here = localKey else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.message = L("选放着模型的文件夹（里面一个模型一个子文件夹，MLX 格式）")
        panel.prompt = L("添加")
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let online = (node(here)?.active ?? []).map { title($0, on: here) }.joined(separator: L("、"))
        if !online.isEmpty {
            guard confirm(L("添加这个文件夹？"), L("oMLX 改模型目录时会卸载已加载的模型（\(online)），正在答的请求会中断；加完要再加载一次。\n\n\(url.path)"), ok: L("卸载并添加")) else { return }
        }
        perform(L("添加模型文件夹"), ["engine", "models-dir", "add", url.path, "yes=1"], summary: true)
    }

    /// 删除就是从 oMLX 里吊销，不能恢复；只想暂时不让用，用停用（0.5.1 B1）
    func revokeSubKey(_ name: String) {
        guard confirm(L("删除子密钥「\(name)」？"), L("从 oMLX 里删掉，不能恢复；用这把子密钥的客户端会立刻无法调用（\(every)都是）。只想暂时不让它用，点停用。"), ok: L("删除"), critical: true) else { return }
        perform(L("删除子密钥「\(name)」"), ["subkey", "revoke", name, "--yes"], showLog: false)
    }

    /// 停用、启用只改入口的停用名单，密钥还在 oMLX 里；立刻生效，不用确认
    func setSubKey(_ name: String, disabled: Bool) {
        perform(disabled ? L("停用子密钥「\(name)」") : L("启用子密钥「\(name)」"), ["subkey", disabled ? "disable" : "enable", name], showLog: false)
    }

    func resetMainKey() {
        guard confirm(L("重置主密钥？"), L("旧主密钥立刻失效，用主密钥的客户端都要换成新的；子密钥不受影响。单独配对的机器有自己的主密钥，不受影响。新主密钥会复制到剪贴板，30 秒后清空。已经登录的网页不会被踢下线。"),
                      ok: L("重置"), critical: true) else { return }
        perform(L("重置主密钥"), ["key", "reset", "--yes"], showLog: false)
    }

    func togglePanel(_ which: BottomPanel) {
        panel = panel == which ? nil : which
        if panel == .logs { refreshServerLog() }
    }

    /// oMLX 的日志：只读，后台取，不占操作位，取的时候别的按钮照常能点
    func refreshServerLog() {
        guard !serverLogLoading else { return }
        serverLogLoading = true
        DispatchQueue.global().async {
            let result = Backend.run(["logs"])
            let lines = (result.code == 0 ? result.out : result.err).components(separatedBy: "\n").suffix(400)
            DispatchQueue.main.async {
                self.serverLog = lines.map { LogLine(text: $0, isError: false) }
                self.serverLogLoading = false
            }
        }
    }

    // MARK: 电源（用户 11-10 定：每张卡片都有关机，主机还有全部关机）

    func powerOff(_ key: String) {
        guard canPowerOff(key), running == nil else { return }
        if Backend.isManager { managerPowerOff(key); return }
        let here = node(key)?.local == true
        let text = here ? L("会先卸载模型、停止 oMLX，再关这台 Mac。这台上别的应用不会提示保存，未保存的内容会丢。") + "\n\n" + afterPowerOff(key)
                        : L("会先卸载模型、停止 oMLX，再关机。这台上别的应用不会提示保存，未保存的内容会丢。关机后只能到机器上按电源键才能再开。\n\n")
        guard confirm(L("关机 \(machine(key))？"), text + runningText([key]) + usage([key]), ok: L("关机"), critical: true),
              let proof = topProof(["chip", "payload", "poweroff", key], intent: .poweroff(key), reason: L("关机 \(machine(key))"), needsPassword: false),
              let password = passwordIfSet(L("关机 \(machine(key))")) else { return }
        let done = track(key, L("正在关机…"))
        perform(L("关机 \(machine(key))"), ["poweroff", key, "--yes"], input: password + "\n" + proof + "\n", onDone: done)
    }

    /// 全部关机（用户 11-10 定，决定 9）：只在主机上。先关副机，确认关掉再关这台；有副机关不掉，这台就留着
    func powerOffAll() {
        if Backend.isManager { managerPowerOff(nil); return }
        guard localIsHub, running == nil, let me = localKey else { return }
        let subs = nodeOrder.filter { $0 != me && node($0)?.reachable == true && node($0)?.isHub != true }
        let locked = nodeOrder.filter { $0 != me && node($0)?.reachable == false && node($0)?.waiting_unlock == true }
        var order = subs.isEmpty ? L("没有开着的副机，直接关 \(machine(me))。")
                                 : L("先关 \(subs.map(machine).joined(separator: L("、")))，确认关掉后再关 \(machine(me))；有副机关不掉，\(machine(me)) 就不关，让你看得到。")
        if !locked.isEmpty { order += "\n" + L("\(locked.map(machine).joined(separator: L("、"))) 开着在等解锁，这次不关它（它的 oMLX 本来就没在跑）；要关就到机器前，在登录界面点关机。") }
        guard confirm(L("全部关机？"), order + "\n\n" + afterPowerOff(me) + runningText(nodeOrder) + usage(nodeOrder), ok: L("全部关机"), critical: true),
              let proof = topProof(["chip", "payload", "poweroff", "all"], intent: .poweroff(nil), reason: L("全部关机"), needsPassword: false),
              let password = passwordIfSet(L("全部关机")) else { return }
        let done = track(nil, L("正在关机…"))
        perform(L("全部关机"), ["poweroff", "all", "--yes"], input: password + "\n" + proof + "\n", onDone: done)
    }

    /// 这台关机后怎么回来。主机这句是用户 11-10 定的（已定第 24 条，按第 26 条改成"开机并登录"：开了自动登录的 Mac 不用输密码）；"登录时打开"没开、"打开时启动 oMLX"关着时照实说
    private func afterPowerOff(_ key: String) -> String {
        let name = machine(key)
        let back = L("关机后，要到 \(name) 前按电源键开机并登录。")
        let then: String
        if !localIsHub { then = L("模型要你再加载。") }
        else if SMAppService.mainApp.status != .enabled { then = L("登录后要你打开控制台（设置 → 通用 → 启动 能设成登录时自动打开），模型要你再加载。") }
        else if !autoStart { then = L("登录后控制台会打开；oMLX 要你点启动，模型要你再加载。") }
        else { then = L("登录后控制台会打开并启动 oMLX，模型要你再加载。") }
        let others = localIsHub && nodeOrder.count > 1 ? "\n" + L("别的机器不跟着关，但客户端要等这台回来，才能经这台用它们的模型。") : ""
        return back + then + others + "\n" + L("关机后这个窗口会跟着关掉，看不到结果。") + "\n\n"
    }

    /// 关机前说清每台还在跑什么：在线的模型、谁在做什么操作（请求数见 usage）
    private func runningText(_ keys: [String]) -> String {
        let lines = keys.compactMap { key -> String? in
            guard let n = node(key), n.reachable else { return nil }
            var parts: [String] = []
            if !n.active.isEmpty { parts.append(L("运行 ") + n.active.map { title($0, on: key) }.joined(separator: L("、"))) }
            else if n.up { parts.append(L("服务开着，没有模型")) }
            if let lock = n.lock { parts.append(L("被 \(lock.who ?? L("另一个控制台")) 占用\(lock.what.map { L("：\($0)") } ?? "")")) }
            return parts.isEmpty ? nil : L("\(machine(key))：\(parts.joined(separator: L("；")))")
        }
        return lines.isEmpty ? "" : lines.joined(separator: "\n") + "\n"
    }

    /// 管理版的关机（用户 11-04 定）：一台或全部，主机也能关，不等请求答完。
    /// 确认框加系统确认；开了安全芯片由被关的那台核对签名，设了最高密码由主机核对
    func managerPowerOff(_ key: String?) {
        guard running == nil else { return }
        let name = key.map(machine) ?? L("全部机器")
        let withHub = key == nil || node(key!)?.isHub == true
        let plan = key == nil ? L("副机先关，确认关掉后再关主机；有副机关不掉，主机就留着，让你看得到。")
                 : withHub ? L("这台是主机，关掉后这边连不上任何一台，副机的模型也用不了入口。")
                 : L("会先卸载模型、停止 oMLX，再关机。")
        guard confirm(L("关机 \(name)？"), plan + L("不等请求答完，对话会中断。关机后远程开不回来，只能到机器上按电源键。\n\n") + usage(key.map { [$0] } ?? nodeOrder), ok: L("关机"), critical: true),
              let proof = topProof(["chip", "payload", "poweroff", key ?? "all"], intent: .poweroff(key), reason: L("远程关机：\(name)"), needsPassword: false),
              let password = passwordIfSet(L("关机 \(name)")) else { return }
        perform(L("关机 \(name)"), ["hub", "poweroff", key ?? "all"], input: password + "\n" + proof + "\n", onDone: { [weak self] ok in
            if ok && withHub { self?.hubOff = true }
        })
    }

    /// 开机是人工按键；开机后的解锁打开终端走苹果的 SSH 解锁，密码不经过控制台
    func unlockMachine(_ key: String) {
        unlocking.insert(key)
        unlockSince[key] = Date()
        if Backend.isManager {
            if let host = knownHosts.first(where: { $0.name == key }) { unlockDirect(host) }
            else { _ = confirm(L("还没连接过 \(machine(key))"), L("先点左下角的连接，把这台加进来。"), ok: L("好")) }
            return
        }
        perform(L("打开 \(machine(key)) 的解锁终端"), ["unlock", key], onDone: { [weak self] ok in
            if !ok { self?.unlocking.remove(key) }
        })
    }

    // MARK: 退出（用户 11-04 定）：设备版和 oMLX 同进退，退出才停，崩溃、强退不停。
    // 主机是唯一入口，主机退出就是关闭全部；副机退出只关它自己

    func askExit() -> NSApplication.TerminateReply {
        // 管理版只是遥控器，退出什么都不停
        guard !Backend.isManager, anyUp || anyStarting else { return .terminateNow }
        NSApp.activate(ignoringOtherApps: true)
        let all = localIsHub && nodeOrder.count > 1
        let keys = all ? nodeOrder : (localKey.map { [$0] } ?? [])
        let alert = NSAlert()
        alert.messageText = all ? L("退出并关闭全部？") : L("退出并关闭 \(localMachine) 的 oMLX？")
        let lines = keys.compactMap { key -> String? in
            guard let n = node(key), n.up else { return nil }
            return L("\(machine(key))：\(n.active.isEmpty ? L("服务开着，没有模型") : L("运行 ") + n.active.map { title($0, on: key) }.joined(separator: L("、")))")
        }.joined(separator: "\n")
        alert.informativeText = (all ? (Backend.showsHub ? L("退出后所有机器的 oMLX 和副机的控制台都会关闭，模型卸载，机器本身不关机。想让别的机器接手，先在「主机」里移交。")
                                                          : L("退出后所有机器的 oMLX 和副机的控制台都会关闭，模型卸载，机器本身不关机。"))
                                     : L("控制台和 oMLX 同进退：退出后这台的模型卸载、oMLX 停止，机器本身不关机。主机可以再把它打开。"))
            + "\n\n" + lines + "\n\n" + usage(keys) + (quitOnClose ? L("\n\n只想收起窗口：点右上角的「长条」，或到设置 → 通用关掉「关闭窗口时退出控制台」。")
                                                                 : L("\n\n只想收起窗口，点红色关闭按钮就行，服务继续运行。"))
        alert.alertStyle = .warning
        alert.addButton(withTitle: L("取消"))   // 默认按钮：误按回车不会停服务
        alert.addButton(withTitle: all ? L("关闭全部并退出") : L("关闭并退出"))
        guard alert.runModal() == .alertSecondButtonReturn else { return .terminateCancel }
        // 确认框开着的时候可能有别的操作开始了（比如解锁回来的机器自动启动）：这次先不退
        if let busy = running {
            _ = confirm(L("「\(busy)」刚开始，这次没有退出"), L("等它做完再退出。"), ok: L("好"))
            return .terminateCancel
        }
        showMainWindow()
        let done = track(all ? nil : localKey, L("服务关闭中…"))
        // 一步失败就不退出，错误留在操作记录里
        perform(all ? L("关闭全部") : L("关闭 \(localMachine)"), ["down", all ? "all" : (localKey ?? "all"), "--yes"], onDone: { ok in
            done(ok)
            NSApp.reply(toApplicationShouldTerminate: ok)
        })
        return .terminateLater
    }

    // MARK: 窗口大小：布局按钮和底部展开

    var fitLabel: String { nodeOrder.count < 2 ? L("紧凑") : fitSideBySide ? L("并排") : L("上下排") }
    var fitIcon: String { nodeOrder.count < 2 ? "arrow.down.right.and.arrow.up.left" : fitSideBySide ? "rectangle.split.2x1" : "rectangle.split.1x2" }

    /// 收成这种排法看得全所有机器的最小尺寸，左上角不动。点一下并排，再点上下排，循环
    func fitWindow() {
        guard let window = mainWindow else { return }
        let sideBySide = nodeOrder.count > 1 && fitSideBySide
        let width = (sideBySide ? CGFloat(nodeOrder.count) * (Layout.cardMin + Layout.gap) - Layout.gap : Layout.cardMin) + Layout.margin * 2
        if nodeOrder.count > 1 { fitSideBySide.toggle() }
        setContent(window, width: width, height: nil)
        // 列数变了要等 SwiftUI 重新排一次，才量得到这种排法的高度
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            guard let self = self else { return }
            self.setContent(window, width: width, height: self.barsHeight + self.gridHeight + Layout.gap * 2 + Layout.margin * 2)
        }
    }

    private func setContent(_ window: NSWindow, width: CGFloat, height: CGFloat?) {
        let old = window.frame
        let visible = window.screen?.visibleFrame ?? old
        // 标题栏和工具栏的高度：frameRect(forContentRect:) 不算工具栏
        let chrome = old.height - window.contentLayoutRect.height
        var frame = NSRect(x: 0, y: 0, width: width, height: (height ?? window.contentLayoutRect.height) + chrome)
        frame.size.height = min(frame.height, visible.height)
        frame.origin.x = max(visible.minX, min(old.minX, visible.maxX - frame.width))
        frame.origin.y = max(visible.minY, old.maxY - frame.height)
        window.setFrame(frame, display: true)
    }

    /// 展开时窗口往下长出这一块，收起时收回同样多，窗口顶部不动；屏幕放不下就只长到屏幕边
    private func resizeForPanel(opening: Bool) {
        guard let window = mainWindow, window.isVisible else { panelGrowth = 0; return }
        let start = window.frame
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let self = self else { return }
            let visible = window.screen?.visibleFrame ?? start
            let height = opening ? min(start.height + Layout.panel, visible.height) : window.frame.height - self.panelGrowth
            self.panelGrowth = opening ? height - start.height : 0
            window.setFrame(NSRect(x: start.minX, y: max(visible.minY, start.maxY - height), width: window.frame.width, height: height), display: true)
        }
    }

    // 内存超线横条上的补救按钮，先确认
    func freeOllama() {
        let loaded = status?.ollama ?? []
        guard !loaded.isEmpty else { return }
        let names = loaded.map { L("\($0.name)（\($0.size_gb) GB）") }.joined(separator: L("、"))
        guard confirm(L("让 Ollama 释放内存？"), hasDsh ? L("会请 Ollama 卸载 \(names)。下次在 Ollama 或 dsh 里用到时，它会自动重新加载。") : L("会请 Ollama 卸载 \(names)。下次在 Ollama 里用到时，它会自动重新加载。"),
                      ok: L("释放")) else { return }
        perform(L("让 Ollama 释放内存"), ["ollama", "free", "--yes"])
    }

    /// 设置（原来的管理页，用户 11-04 定）：点一下直接进去。控制台向那台换一个登录会话，放进应用里的窗口；
    /// 外部浏览器没法安全地接过登录状态，密钥也不能放进网址。主密钥不离开那台机器
    /// settings：B3 的"打开 oMLX 设置"，直接到管理页的设置页（官方 0.8.2 和两台自管的都认 `?tab=settings`；`/admin` 跳转时会丢掉参数，所以写全 `/admin/dashboard`）
    func openAdmin(_ n: String, settings: Bool = false) {
        let page = settings ? "/dashboard?tab=settings" : nil
        if AdminWindows.shared.raise(n, page: page) { return }
        DispatchQueue.global().async {
            let r = Backend.run(["admin-session", n])
            let session = try? JSONDecoder().decode(AdminSession.self, from: Data(r.out.utf8))
            DispatchQueue.main.async {
                if let s = session, s.ok {
                    AdminWindows.shared.open(n, title: L("\(self.machine(n)) · oMLX 管理页"), session: s, page: page)
                } else if self.confirm(L("没能自动登录 \(self.machine(n))"), L("可以在浏览器里打开管理页，自己输主密钥。"), ok: L("在浏览器打开")),
                          let url = self.status?.pages?[n].flatMap({ URL(string: $0 + (page ?? "")) }) {
                    NSWorkspace.shared.open(url)
                }
            }
        }
    }

    // MARK: 长条

    func collapseToBar() {
        BarPanel.shared.show(self)
        for window in NSApp.windows where Self.isMain(window) { window.orderOut(nil) }
    }

    func expandFromBar() {
        BarPanel.shared.hide()
        showMainWindow()
    }

    /// A9（已定第 31 条）：确认框挂在当前窗口上，成为下拉框；窗口收起、是长条、最小化，或已经有一个框开着时，照旧弹在屏幕中间。
    /// 调用的地方照旧同步拿到结果：先挂上去，再在这个框上跑一轮模态，按钮一按就结束。退出控制台的两个框不走这里（窗口可能已经关了）
    static func present(_ alert: NSAlert) -> NSApplication.ModalResponse {
        NSApp.activate(ignoringOtherApps: true)
        guard NSApp.modalWindow == nil, var host = sheetHost() else { return alert.runModal() }
        while let sheet = host.attachedSheet { host = sheet }
        alert.beginSheetModal(for: host) { NSApp.stopModal(withCode: $0) }
        return NSApp.runModal(for: alert.window)
    }

    /// 挂在哪个窗口上：现在在用的那个（主窗口、设置、安装窗口）；都不在前面就挂主窗口；主窗口也看不见就不挂
    private static func sheetHost() -> NSWindow? {
        let usable = { (w: NSWindow) -> Bool in w.isVisible && !w.isMiniaturized && !(w is NSPanel) && w.styleMask.contains(.titled) }
        if var key = NSApp.keyWindow {
            while let parent = key.sheetParent { key = parent }
            if usable(key) { return key }
        }
        return NSApp.windows.first { isMain($0) && usable($0) }
    }

    func confirm(_ title: String, _ text: String, ok: String, critical: Bool = false) -> Bool {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = text
        alert.alertStyle = critical ? .critical : .warning
        alert.addButton(withTitle: ok)
        alert.addButton(withTitle: L("取消"))
        return Console.present(alert) == .alertFirstButtonReturn
    }
}

// MARK: - 通用小部件

struct Dot: View {
    let color: Color
    var size: CGFloat = 10
    var body: some View { Circle().fill(color).frame(width: size, height: size) }
}

/// 灰边小标签：本机
struct Tag: View {
    let text: String
    var body: some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(Color.secondary.opacity(0.45), lineWidth: 0.5))
            .fixedSize()
    }
}

/// 标签列宽固定，几行信息上下对齐
struct InfoRow<Content: View>: View {
    let label: String
    @ViewBuilder let content: Content
    var body: some View {
        GridRow {
            Text(label).foregroundStyle(.secondary).gridColumnAlignment(.trailing)
            content.gridColumnAlignment(.leading)
        }
    }
}

/// 内存条：实色是已用，浅色延伸到"对话塞满时最多约"，红色竖线是安全线
struct MemoryBar: View {
    let used: Double
    var most: Double? = nil
    let total: Double
    let line: Double?
    let tint: Color
    let alert: Bool
    var warn = false

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let scale = { (v: Double) in width * CGFloat(min(max(v / max(total, 1), 0), 1)) }
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.2))
                if let most = most, most > used {
                    Capsule().fill((warn ? Color.orange : tint).opacity(0.35)).frame(width: max(4, scale(most)))
                }
                Capsule().fill(alert ? Color.red : tint).frame(width: max(4, scale(used)))
                if let line = line {
                    Rectangle().fill(Color.red).frame(width: 2, height: geo.size.height + 6).offset(x: scale(line) - 1)
                }
            }
        }
    }
}

// MARK: - 窗口用到的尺寸

enum Layout {
    static let cardMin: CGFloat = 530            // 一列的最小宽度：顶部五个按钮、卡片四个按钮和底部一行都是常规大小，放得下
    static let gap: CGFloat = 14
    static let margin: CGFloat = 18
    static let panel: CGFloat = 228              // 底部展开那块，加上和上面一行的间距
}

// MARK: - 主窗口：一行总控 → 每台一列（机器状态 + 模型卡片）→ 一行工具和操作记录

private struct CardHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct GridHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

private struct BarsHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value += nextValue() }
}

private extension View {
    func measured<K: PreferenceKey>(_ key: K.Type) -> some View where K.Value == CGFloat {
        background(GeometryReader { Color.clear.preference(key: key, value: $0.size.height) })
    }
}

/// 窗口能调的范围（用户 11-02 定）：
/// · 往宽拉多台并排，往窄拉一列一台、上下滚动看其他机器；最窄也放得下一台的全部操控。
/// · 往矮拉，最小是一台机器的完整界面，其他机器往下滚。
/// · 往大拉到每台都看全、留一点余量就停，不留大片空白。
/// 第 6 期：这台上没有能用的 Python 时，后端跑不起来，主窗口先给一页说明；有了就是平时的界面
struct MainGate: View {
    @State private var ready = Backend.pythonPath != nil
    @State private var note = ""
    var body: some View {
        if ready { ContentView() } else {
            VStack(alignment: .leading, spacing: 14) {
                Text(L("先装 oMLX")).font(.title2.weight(.semibold))
                Text(L("控制台用 oMLX 自带的 Python 运行。这台 Mac 上还没找到 oMLX，也没有别的 Python。"))
                Text(L("两条路选一条：\n1. 装 oMLX（本来也要装）：下载后把它拖进\"应用程序\"，回来点\"重新检查\"。\n2. 装苹果的命令行工具（免费，系统会弹一个安装框，约几分钟）。"))
                    .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    Button(L("打开 oMLX 下载页")) { if let url = URL(string: "https://github.com/jundot/omlx/releases") { NSWorkspace.shared.open(url) } }
                    Button(L("装苹果的命令行工具")) {
                        let p = Process(); p.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select"); p.arguments = ["--install"]
                        p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
                        try? p.run()
                    }
                    Spacer()
                    Button(L("重新检查")) {
                        Backend.pythonPath = Backend.findPython()
                        ready = Backend.pythonPath != nil
                        note = ready ? "" : L("还没找到。装好后再点一次。")
                    }.keyboardShortcut(.defaultAction)
                }
                if !note.isEmpty { Text(note).foregroundStyle(.secondary) }
            }
            .padding(28).frame(width: 560)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow
    @State private var showKeys = false
    @State private var cardHeight: CGFloat = 0   // 现在最高的那台机器卡（文字折行、提示多的时候会比估的高）
    @State private var barsHeight: CGFloat = 0   // 顶部（含红色横条）和底部（含操作记录）加起来
    @State private var areaWidth: CGFloat = 0

    private var count: Int { max(nodeOrder.count, 1) }
    private let cardMin = Layout.cardMin
    private let columnMax: CGFloat = 640         // 一列再宽就只是多出空白
    private let gap = Layout.gap
    private let margin = Layout.margin
    private let peek: CGFloat = 56               // 上下排时多露出下一台的卡片头，一看就知道下面还有
    private let slack: CGFloat = 100             // 看全所有机器后还能多拉开的高度

    /// 一台机器完整的高度：一张模型卡片展开、其余收着。按这个定上下限，点开收起卡片时窗口不跟着跳
    private var cardFull: CGFloat { 388 + CGFloat(max(nodeOrder.map { console.models(on: $0).count }.max() ?? modelOrder.count, 1) - 1) * 44 }   // 按模型最多的那台

    private func columns(_ width: CGFloat) -> Int { max(1, min(count, Int((width + gap) / (cardMin + gap)))) }

    /// 窗口放得下几列就排几列，放不下换行；内容比窗口高时中间这块滚动，顶部和底部两行不动
    private func rows(_ width: CGFloat) -> (Int, [[String]]) {
        let columns = columns(width)
        return (columns, stride(from: 0, to: nodeOrder.count, by: columns).map { Array(nodeOrder[$0..<min($0 + columns, nodeOrder.count)]) })
    }

    var body: some View {
        let perRow = columns(areaWidth), rowCount = (count + perRow - 1) / perRow
        let chrome = barsHeight + gap * 2 + margin * 2
        let screen = (NSScreen.main?.visibleFrame.height ?? 2000) - 40        // 减标题栏；再大屏幕放不下，靠中间滚动
        let least = min(chrome + max(cardFull, cardHeight) + (rowCount > 1 ? peek : 0), screen)
        let most = max(least, chrome + CGFloat(rowCount) * (cardFull + gap) - gap + slack)
        VStack(alignment: .leading, spacing: gap) {
            VStack(alignment: .leading, spacing: gap) {
                if Backend.isManager && console.statusError != nil { ManagerRescue() }
                if console.overLine { AlertBanner() }
                if console.stuck { StallBanner() }
                if let e = console.engine, e.warnsVersion { EngineBanner(engine: e) }
                ForEach(console.visibleNotices) { NoticeBanner(notice: $0) }
                if let row = console.consoleUpdate { ConsoleUpdateBanner(row: row) }
                TopBar()
            }
            .measured(BarsHeightKey.self)
            GeometryReader { geo in
                let (columns, grid) = rows(geo.size.width)
                ScrollView(.vertical) {
                    VStack(spacing: gap) {
                        ForEach(grid, id: \.self) { row in
                            HStack(alignment: .top, spacing: gap) {
                                ForEach(row, id: \.self) { MachineCard(key: $0).measured(CardHeightKey.self) }
                                ForEach(row.count..<columns, id: \.self) { _ in Color.clear.frame(maxWidth: .infinity, maxHeight: 0) }
                            }
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .measured(GridHeightKey.self)
                }
                .scrollIndicators(.visible)
                .onAppear { areaWidth = geo.size.width }
                .onChange(of: geo.size.width) { areaWidth = geo.size.width }
            }
            BottomBar(showKeys: $showKeys).measured(BarsHeightKey.self)
        }
        .padding(margin)
        .onPreferenceChange(CardHeightKey.self) { cardHeight = $0 }
        .onPreferenceChange(BarsHeightKey.self) { barsHeight = $0; console.barsHeight = $0 }
        .onPreferenceChange(GridHeightKey.self) { console.gridHeight = $0 }
        .frame(minWidth: cardMin + margin * 2, idealWidth: CGFloat(count) * (cardMin + gap) - gap + margin * 2 + 20,
               maxWidth: CGFloat(count) * (columnMax + gap) - gap + margin * 2, minHeight: least, idealHeight: least, maxHeight: most)
        .sheet(isPresented: $showKeys) { KeySheet(shown: $showKeys).environmentObject(console) }
        .onAppear { console.openWindowAction = openWindow; DispatchQueue.main.async { console.applyPin() } }
        // 右上角和左上角的系统按钮齐平（用户 11-04 定）；A4 改成 设置 / 排列 / 长条 / 固定（已定第 31 条）
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                SettingsLink { Label(L("设置"), systemImage: "gearshape").labelStyle(.titleAndIcon) }
                    .help(L("控制台的设置：通用、更新、教程、关于、oMLX"))
                Button { console.fitWindow() } label: { Label(L("排列"), systemImage: console.fitIcon).labelStyle(.titleAndIcon) }
                    .help(nodeOrder.count < 2 ? L("收成看得全这台的最小窗口") : L("收成\(console.fitLabel)时看得全\(console.every)的最小窗口；再点一下换另一种排法"))
                Button { console.collapseToBar() } label: { Label(L("长条"), systemImage: "arrow.down.right.and.arrow.up.left").labelStyle(.titleAndIcon) }
                    .help(L("收起成浮动长条，主窗口隐藏；点长条右边的按钮展开回来"))
                Toggle(isOn: $console.pinned) { Label(L("固定"), systemImage: console.pinned ? "pin.fill" : "pin").labelStyle(.titleAndIcon) }
                    .toggleStyle(.button)
                    .help(console.pinned ? L("已固定：窗口钉在原处，总在别的窗口前面。再点一下取消") : L("把窗口钉在原处，总在别的窗口前面；再点一下取消"))
            }
        }
    }
}

struct AlertBanner: View {
    @EnvironmentObject var console: Console

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "exclamationmark.triangle.fill").font(.system(size: 30)).foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 6) {
                Text(L("内存超线")).font(.title2.bold())
                ForEach(console.memoryAlert, id: \.self) { Text($0).font(.headline) }
                Text(console.status?.advice ?? "").fixedSize(horizontal: false, vertical: true)
            }
            .foregroundStyle(.white)
            Spacer()
            VStack(alignment: .trailing, spacing: 8) {
                if console.hasOllama {
                    Button { console.freeOllama() } label: { Text(L("让 Ollama 释放…")).frame(width: 150) }
                        .disabled(console.running != nil || (console.status?.ollama ?? []).isEmpty)
                }
                Button { console.unloadAll() } label: { Text(L("卸载全部模型…")).frame(width: 150) }
                    .disabled(console.busy || !console.anyLoaded)
            }
            .controlSize(.large)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.red))
    }
}

struct StallBanner: View {
    @EnvironmentObject var console: Console

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: "pause.circle.fill").font(.system(size: 30)).foregroundStyle(.white)
            VStack(alignment: .leading, spacing: 6) {
                Text(L("可能卡住了")).font(.title2.bold())
                Text(console.progress?.text ?? "").font(.headline).fixedSize(horizontal: false, vertical: true)
                Text(L("每分钟检查一次；有新进度后这条会自动消失。")).font(.callout)
            }
            .foregroundStyle(.white)
            Spacer()
            Button { console.checkProgress() } label: { Text(L("现在再查一次")).frame(width: 150) }
                .controlSize(.large)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange))
    }
}

/// oMLX 的版本要注意（太旧、有已知问题、读不到版本、没有兼容表）：一行不挡操作的提示，"查看"直接到设置的更新页
struct EngineBanner: View {
    let engine: EngineInfo

    private var text: String {
        let verdict = engine.verdict_text ?? ""
        let head = engine.version.map { L("这台的 oMLX 是 \($0)，\(verdict)") } ?? L("这台的 oMLX：\(verdict)")
        return head + (engine.note.map { L("：\($0)") } ?? "")
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "exclamationmark.circle.fill").foregroundStyle(.orange)
            Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            SettingsButton(title: L("查看"), tab: Backend.isManager ? .omlx : .updates).controlSize(.small)   // 升级按钮在更新页；管理端没有更新页
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.12)))
    }
}

/// 官方版没允许读主密钥时，本机卡片里说清楚，并能当场允许
struct KeyBlockedHint: View {
    @EnvironmentObject var console: Console
    let engine: EngineInfo

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "key.fill").foregroundStyle(.orange)
            Text(engine.key == "denied" ? L("没允许控制台读这台 oMLX 的主密钥，管理功能用不了：看不到模型，也不能加载、卸载、管理子密钥、看日志和多轮缓存。")
                                        : L("还没允许控制台读这台 oMLX 的主密钥：允许以后才看得到模型、能加载和卸载。"))
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            Button(L("允许…")) { console.askEngineKeyNow() }.buttonStyle(.bordered)
        }
    }
}

/// 顶部：左边标题和状态行，右边全部操作。按钮不消失，不能按时灰着（用户 11-04 定）。操作进行中多一行进度
/// B2（已定第 31 条）：标题一行（这台的名字 + 单机 / 主机 · N 台 / 副机；管理版是"管理版" + 连着的主机），状态一行（状态点 + 在跑什么 · 更新时间）
struct TopBar: View {
    @EnvironmentObject var console: Console

    private var many: Bool { nodeOrder.count > 1 }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(console.headTitle).font(.system(size: 20, weight: .semibold)).lineLimit(1)
                if !console.headTag.isEmpty { Tag(text: console.headTag) }
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Dot(color: console.headLevel.color, size: 8)
                Text(console.headLine).font(.system(size: 13)).monospacedDigit().foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .help(console.status?.mode_text ?? "")
        }
    }

    @ViewBuilder
    private var startAll: some View {
        let label = Label(L("启动全部"), systemImage: "play.fill")
        if console.anyDown && !console.busy && !console.stale {
            Button { console.startService() } label: { label }.buttonStyle(.borderedProminent)
        } else {
            Button { console.startService() } label: { label }.disabled(true)
        }
    }

    private var firstButtons: some View {
        HStack(spacing: 8) {
            if many {
                startAll.help(L("启动所有机器的 oMLX 和副机的控制台，不加载模型"))
                Button { console.closeAll() } label: { Label(L("关闭全部"), systemImage: "stop.circle") }
                    .disabled(console.busy || console.stale || !(console.anyUp || console.anyStarting))
                    .help(Backend.isManager ? L("关闭所有机器的 oMLX 和副机的控制台") : L("关闭所有机器的 oMLX 和副机的控制台，这个窗口也退出"))
            }
            Button { console.unloadAll() } label: { Label(L("卸载全部"), systemImage: "eject") }
                .disabled(console.busy || console.stale || !console.anyLoaded)
                .help(L("卸载所有模型，服务保持运行"))
        }
    }

    private var secondButtons: some View {
        HStack(spacing: 8) {
            if many {
                Button { console.restartService() } label: { Label(L("重启全部"), systemImage: "arrow.clockwise.circle") }
                    .disabled(console.busy || console.stale || !console.anyUp)
                    .help(L("重启所有机器的 oMLX，刷新所有控制台"))
            }
            if Backend.isManager || (many && console.localIsHub) {
                Button { console.powerOffAll() } label: { Label(L("全部关机"), systemImage: "power") }
                    .disabled((Backend.isManager ? console.running != nil : console.busy) || console.stale || !nodeOrder.contains { console.node($0)?.reachable == true })
                    .help(L("副机先关，确认关掉后再关主机。关机后只能到机器上按电源键才能再开"))
            }
            Button { console.refreshNow() } label: { Label(many ? L("刷新全部") : L("刷新"), systemImage: "arrow.clockwise") }
                .disabled(console.refreshing)
                .help(console.refreshing ? L("刷新中…") : L("立刻刷新所有机器的状态"))
        }
    }

    private var buttons: some View {
        HStack(spacing: 8) { firstButtons; secondButtons }.buttonStyle(.bordered).fixedSize()
    }

    /// 六个按钮，一行放不下时分两行，不缩小
    private var twoRows: some View {
        VStack(alignment: .leading, spacing: 8) { firstButtons; secondButtons }.buttonStyle(.bordered).fixedSize()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // 放得下就标题在左、按钮在右；窄的时候按钮换到标题下面，状态行折成两行，不截断
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .top, spacing: 10) {
                    heading
                    Spacer(minLength: 8)
                    buttons
                }
                VStack(alignment: .leading, spacing: 10) {
                    heading
                    buttons
                }
                VStack(alignment: .leading, spacing: 10) {
                    heading
                    twoRows
                }
            }
            if let running = console.running {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    TimelineView(.periodic(from: .now, by: 1)) { context in
                        let seconds = Int(context.date.timeIntervalSince(console.runningSince ?? context.date))
                        Text(L("正在\(running)… 已用 \(seconds) 秒")).foregroundStyle(.secondary).monospacedDigit()
                    }
                    if !console.runningLine.isEmpty { Text(console.runningLine).font(.callout).foregroundStyle(.tertiary).lineLimit(1) }
                    if console.cancellable {
                        Button(L("停止等待")) { console.stopWaiting() }.help(L("还在等请求答完，什么都没动；停止后模型保持在线"))
                    } else {
                        // 很久没有任何新输出（多半是后端挂住了）才出现
                        TimelineView(.periodic(from: .now, by: 5)) { context in
                            if context.date.timeIntervalSince(console.lastActivity ?? context.date) >= Console.quietLimit {
                                Button(L("强制结束…")) { console.forceStop() }.help(L("很久没有新输出了。只是让控制台不再等，机器上的动作可能还在继续"))
                            }
                        }
                    }
                }
            }
            if let error = console.statusError { Text(L("读取状态出错：\(error)")).foregroundStyle(.red) }
        }
        .padding(.horizontal, 2)
    }
}

/// 一台机器一列：机器状态、内存和多轮缓存，下面是模型卡片
struct MachineCard: View {
    @EnvironmentObject var console: Console
    let key: String
    @State private var picked: String?      // 用户点开的卡片；nil 时展开在线的那张
    @State private var closed = false       // 用户把展开的那张收起来了

    /// 在线或加载中的模型；变了就回到默认（展开在线的那张）
    private var current: String? { console.node(key).flatMap { $0.loading ?? $0.loaded } }

    private func isOpen(_ m: String) -> Bool { !closed && m == (picked ?? current) }

    private func toggle(_ m: String) {
        if isOpen(m) { closed = true; picked = nil } else { closed = false; picked = m }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            if console.node(key)?.local == true, console.firstRunVisible, !console.stale, let info = console.firstRun { SetupGuide(key: key, info: info) }
            if let n = console.node(key), n.used_gb != nil, n.reachable, !console.stale { machineInfo }   // 关机、连不上时不显示旧的内存
            if let n = console.node(key), n.reachable, n.up, !console.stale {
                if n.local == true, let e = console.engine, e.keyBlocked { KeyBlockedHint(engine: e) }
                // 按这个控制台调好的顺序，默认新模型靠前，位置不随加载卸载跳动；每台同时只展开一张
                VStack(spacing: 8) {
                    ForEach(console.models(on: key), id: \.self) { m in
                        ModelCard(key: key, m: m, n: n, open: isOpen(m)) { toggle(m) }
                    }
                }
            } else if console.node(key)?.local == true, console.engine?.kind == "none", !console.stale {
                if !console.firstRunVisible { InstallHint() }   // 第 3b 期：这台没装 oMLX；自管版没有 engine，走不到这里。有"开始使用"时由它的第一步来说
            } else {
                placeholder
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(RoundedRectangle(cornerRadius: 12).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.primary.opacity(0.08)))
        .onChange(of: current) { picked = nil; closed = false }
    }

    /// 名字｜主/副机 + 本机小标签（用户 11-04 定）；下面一行是状态点和说明
    private var identity: some View {
        let n = console.node(key)
        let state = console.dot(key)
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(console.machine(key)).font(.system(size: 15, weight: .semibold)).lineLimit(1)
                if let role = n?.role, nodeOrder.count > 1 || Backend.isManager {   // 只有一台时不写"主机"：顶部已经标了"单机"，两个说法摆在一起让人糊涂（界面验收 11-22）
                    Text(L("｜")).foregroundStyle(.tertiary)
                    Text(role).foregroundStyle(.secondary).fixedSize()
                }
                if n?.local == true && !Backend.isManager { Tag(text: L("本机")) }   // 管理版读到的 local 是主机自己
                if n?.local != true && n?.reachable == true && n?.desktop == false && !console.stale {
                    Tag(text: L("没登录桌面")).help(L("这台停在登录界面：它的控制台窗口打不开，oMLX 照常能用。要看它的窗口，到机器前登录"))
                }
            }
            HStack(spacing: 6) {
                Dot(color: state.color, size: 8)
                Text(state.text).font(.callout).foregroundStyle(.secondary).fixedSize()
                if let lock = n?.lock {
                    Text(L("· 被 \(lock.who ?? L("另一个控制台")) 占用\(lock.what.map { L("：\($0)") } ?? "")\(lock.since.map { L("，\($0) 开始") } ?? "")"))
                        .font(.callout).foregroundStyle(.orange).lineLimit(1)
                }
                if n?.code_ok == false {
                    Text(L("· 程序版本不同")).font(.callout).foregroundStyle(.orange).lineLimit(1).help(L("能用，建议更新这台的控制台"))
                }
            }
        }
    }

    private var header: some View {
        // 放不下就把按钮换到第二行，不缩小（用户 11-04 定）
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                identity
                Spacer(minLength: 8)
                machineButtons
            }
            VStack(alignment: .leading, spacing: 10) {
                identity
                machineButtons
            }
        }
        .buttonStyle(.bordered)
    }

    /// 启动/关闭、重启、关机、设置（用户 11-04 定）。每张卡片都有关机（11-10 起）；位置不随状态跳，不能按时灰着
    @ViewBuilder
    private var machineButtons: some View {
        let n = console.node(key)
        let service = n?.service ?? "down"
        let reachable = n?.reachable != false && !console.stale
        HStack(spacing: 8) {
            if !reachable && n?.waiting_unlock == true && !console.stale {
                Button { console.unlockMachine(key) } label: { Label(L("解锁…"), systemImage: "lock.open") }
                    .buttonStyle(.borderedProminent)
                    .disabled(console.busy)
                    .help(L("打开终端，把 \(console.machine(key)) 的 macOS 密码直接输给系统；解锁后自动启动 oMLX，不加载模型"))
            } else if service == "down" || !reachable {
                let label = Label(L("启动"), systemImage: "play.fill")
                if reachable && !console.busy && n?.power != "going_down" {
                    Button { console.startService(key) } label: { label }.buttonStyle(.borderedProminent)
                        .help(n?.local == true ? L("启动这台的 oMLX，不加载模型") : L("启动它的 oMLX 和控制台，不加载模型"))
                } else {
                    Button { console.startService(key) } label: { label }.disabled(true)
                }
            } else {
                Button { console.stopService(key) } label: { Label(L("关闭"), systemImage: "stop.circle") }
                    .disabled(console.busy)
                    .help(n?.local == true && console.localIsHub ? L("卸载模型、停止 oMLX；窗口留着") : n?.local == true ? L("控制台和 oMLX 一起退出") : L("卸载模型、停止 oMLX，它的控制台也退出；机器还开着"))
            }
            Button { console.restartService(key) } label: { Label(L("重启"), systemImage: "arrow.clockwise") }
                .disabled(console.busy || service != "up" || !reachable)
                .help(L("重启 oMLX 和控制台；确认时可以选把模型加载回来"))
            if console.canPowerOff(key) {
                Button { console.powerOff(key) } label: { Label(L("关机"), systemImage: "power") }
                    .disabled((Backend.isManager ? console.running != nil : console.busy) || !reachable || n?.power == "going_down")
                    .help(n?.power == "stuck" ? L("再发一次关机；还不行就到机器前长按电源键")
                          : n?.local == true ? L("先停 oMLX 再关这台 Mac；这个窗口会跟着关掉")
                          : L("先停 oMLX 再关机；关机后只能到机器上按电源键才能再开"))
            }
            Button { console.openAdmin(key) } label: { Label(L("oMLX 管理页"), systemImage: "macwindow") }
                .disabled(n?.up != true || Backend.isManager || !reachable)
                .help(Backend.isManager ? L("管理页要在主机上打开") : L("打开这台的 oMLX 管理页（仪表盘、模型、聊天、日志），控制台代为登录"))
        }
        .fixedSize()
    }

    private var placeholder: some View {
        let n = console.node(key)
        let name = console.machine(key)
        var text: String
        if n == nil { text = L("读取中…") }
        else if console.stale { text = console.hubOff && n?.isHub == true ? L("\(name) 已关机。要开机，请到机器上按电源键。") : L("连不上主机，看不到 \(name) 现在的状态。") }
        else if n?.power == "going_down" { text = L("\(name) 正在关机。") }
        else if n?.power == "stuck" { text = L("\(name) 发出关机 3 分钟了还开着：可能有进程卡住。可以再点一次关机，还不行就到机器前长按电源键。") }
        else if n?.reachable == false {
            text = n?.waiting_unlock == true ? L("\(name) 开着，还没解锁。点上面的解锁，在打开的终端里输入它的 macOS 密码；解锁后自动启动 oMLX。")
                 : n?.power == "off" ? L("\(name) 已关机。要开机，请到机器上按电源键。")
                 : L("连不上 \(name)：可能已关机或断网。开机要到机器上按电源键。")
            if let note = n?.address_note { text += "\n" + note }
        }
        else if n?.service == "starting" { text = L("oMLX 正在启动…") }
        else if n?.service == "reconnecting" { text = L("正在重新连接 \(name)：它的 oMLX 还在跑。换了网线或 Wi-Fi 时会这样，一般十几秒接上。") }
        else { text = L("oMLX 服务没运行。启动后在这里加载模型（启动不会自动加载）。") }
        return Text(text).foregroundStyle(.secondary).frame(maxWidth: .infinity, minHeight: 90)
    }

    /// A5（已定第 31 条 ⑥）：顶上是总数和压力、GPU（旁边 ⓘ 说明），内存条上方是图例，下面一行是整台的数；A10：塞满会超安全线时写明是哪个模型、改到哪一档就不超
    private var machineInfo: some View {
        let n = console.node(key)
        let alert = n?.alert?.isEmpty == false, warn = n?.warn?.isEmpty == false
        let used = Double(n?.used_gb ?? 0), most = Double(n?.max_gb ?? n?.used_gb ?? 0), total = Double(n?.total_gb ?? max(n?.max_gb ?? 0, n?.used_gb ?? 0, 1))   // 读不到总内存不按 512 GB 猜（2b）
        var detail: [String] = []
        if let u = n?.used_gb { detail.append(L("现在用 \(u) GB") + (n?.omlx_now_gb.map { L("（其中 oMLX \($0) GB）") } ?? "")) }
        if most > used { detail.append(L("塞满时最多约 \(Int(most)) GB")) }
        if let disk = n?.disk_free_gb { detail.append(L("硬盘剩 \(diskText(disk))")) }
        if n?.local == true { for o in console.status?.ollama ?? [] { detail.append("Ollama \(o.name) \(o.size_gb) GB") } }
        if let procs = n?.leftover_procs, procs > 0 { detail.append(L("别的推理进程 \(procs) 个（体检会提示）")) }
        return VStack(alignment: .leading, spacing: 9) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(n?.total_gb == nil ? L("\(Int(used)) GB · 读不到总内存") : "\(Int(used)) / \(Int(total)) GB").fontWeight(.semibold).monospacedDigit().foregroundStyle(alert ? Color.red : Color.primary)
                Text(pressureText(n?.pressure)).font(.callout).foregroundStyle(alert ? Color.red : Color.secondary)
                if let gpu = n?.gpu { Text("GPU \(gpu)%").font(.callout).monospacedDigit().foregroundStyle(.secondary) }
                InfoButton(title: L("内存 · \(console.machine(key))"), items: memoryInfo(n))
                Spacer()
            }
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 12) {
                    legend(Capsule().fill(alert ? Color.red : Color.accentColor), L("已用"))
                    if most > used { legend(Capsule().fill((warn ? Color.orange : Color.accentColor).opacity(0.35)), L("塞满时最多")) }
                    if let line = n?.line_gb {
                        HStack(spacing: 4) { Rectangle().fill(Color.red).frame(width: 2, height: 9); Text(L("安全线 \(line) GB")) }
                    }
                }
                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                MemoryBar(used: used, most: most, total: total, line: n?.line_gb.map(Double.init), tint: Color.accentColor, alert: alert, warn: warn)
                    .frame(height: 6)
            }
            Text(detail.joined(separator: " · ")).font(.subheadline).monospacedDigit().foregroundStyle(warn ? Color.orange : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(console.overFixes(key), id: \.model) { fix in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(console.overText(fix, on: key)).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    if let v = fix.fix {
                        Button(L("改成 \(contextLabel(v))")) { console.setContext(fix.model, on: key, v) }
                            .buttonStyle(.bordered).controlSize(.small).fixedSize()
                            .disabled(console.busy)
                            .help(L("当场生效，不用重新加载，对话不中断"))
                    }
                }
            }
            cacheRow
        }
    }

    private func legend<S: View>(_ swatch: S, _ text: String) -> some View {
        HStack(spacing: 4) { swatch.frame(width: 12, height: 6); Text(text) }
    }

    /// 内存那行的 ⓘ（A5、A12）
    private func memoryInfo(_ n: NodeInfo?) -> [[String]] {
        let line = n?.line_gb, total = n?.total_gb
        let percent = line.flatMap { l in total.map { Int((Double(l) / Double(max($0, 1)) * 100).rounded()) } }
        return [
            [L("已用"), L("整台 Mac 已经用掉的内存，和活动监视器的「已使用内存」同一算法\(n?.local == true && console.hasOllama ? L("，含 Ollama") : "")；括号里是 oMLX 进程此刻的实际占用。")],
            [L("压力"), L("macOS 自己报的内存压力，和活动监视器里「内存压力」那条曲线的颜色是一回事。正常就是够用；警告是系统已经在压缩内存；严重是在拿硬盘顶内存，模型会变得极慢。")],
            [L("GPU"), L("显卡此刻有多忙（0–100%）。模型在回答时会很高，空闲时接近 0；一直很高却没有请求在跑，多半是卡住了。")],
            [L("塞满时最多"), L("在线的模型对话都塞满上下文时，整台大约会到多少：oMLX 加载后实测的权重 + 上下文 × 每个 token 要的内存。平时用不到这么多。")],
            [L("安全线"), line.map { l in L("红色竖线\(percent.map { L("，总内存的 \($0)%") } ?? "")（这台 \(l) GB）。已用超过它就报「内存超线」；塞满时会超过它，这里会写明是哪个模型、改到哪一档就不超。") } ?? L("读到这台的总内存后才有。")],
            [L("显存上限"), L("oMLX 管理页里「显存上限」那行如果是红字，可以不管：控制台的安全线本来就比系统默认的显存上限低（512 GB 的 Mac 上是 461 GB 对约 464 GB），模型不会用到那么多。")],
        ]
    }

    /// 多轮缓存跟着机器走：已用 / 上限，旁边就能改上限（含关）、看说明、清空
    private var cacheRow: some View {
        let n = console.node(key)
        let limit = n?.cache_limit_gb, used = n?.cache_gb ?? 0
        let usedText = used < 10 ? String(format: "%.1f", used) : "\(Int(used.rounded()))"
        let recommended = console.status?.cache_recommended
        let name = { (v: Int) in v == 0 ? L("关") : "\(v) GB" + (v == recommended ? L(" · 推荐") : "") }
        let choices = n?.cache_options ?? []
        let autoOK = n?.cache_auto_ok == true   // A14：官方版改成具体档以后还能选回"自动"
        return HStack(spacing: 8) {
            Text(L("多轮缓存")).font(.callout).foregroundStyle(.secondary).fixedSize()
            Picker("", selection: Binding(get: { limit ?? -1 }, set: { v in if v >= 0 || (autoOK && limit != nil) { console.setCache(v, on: key) } })) {
                Text(L("关")).tag(0)
                ForEach(choices, id: \.self) { Text(name($0)).tag($0) }
                if limit == nil || autoOK { Text(limit == nil && n?.cache_auto != true ? L("读不到") : L("自动")).tag(-1) }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .fixedSize()
            .disabled(console.busy || n?.loading != nil)
            .help(L("这台的多轮缓存上限，所有模型共用；关了资料就不写硬盘。改了不用重启服务，模型会卸载再加载回来"))
            InfoButton(title: L("多轮缓存 · \(console.machine(key))"), items: console.status?.cache_info ?? [])
            Text(limit == 0 ? (used > 0 ? L("留着 \(usedText) GB") : "") : limit.map { L("已用 \(usedText) / \($0) GB") } ?? (n?.cache_auto == true ? L("已用 \(usedText) GB · 上限自动") : L("已用 \(usedText) GB")))
                .font(.subheadline).monospacedDigit().foregroundStyle(.secondary).lineLimit(1)
            Spacer(minLength: 8)
            Button(L("清空缓存…")) { console.clearCache(key) }
                .buttonStyle(.bordered)
                .fixedSize()
                .disabled(console.busy || n?.loaded != nil || (n?.cache_gb ?? 0) <= 0)
                .help(n?.loaded == nil ? L("删除这台的多轮缓存") : L("先卸载这台的模型才能清空"))
        }
    }
}

/// 一个模型一张卡片。卡片头：模型、是否在线；点开才是这个模型的全部操控
struct ModelCard: View {
    @EnvironmentObject var console: Console
    let key: String
    let m: String
    let n: NodeInfo
    let open: Bool
    let toggle: () -> Void
    @State private var hovering = false
    @State private var editing = false

    private var state: String { n.models[m] ?? "off" }
    private var title: String { console.title(m, on: key) }
    private var others: [String] { n.active.filter { $0 != m } }
    private var fits: Bool { n.fits?[m] == true }     // 放得下：不卸载别的模型，一起跑
    private var verb: String { others.isEmpty || fits ? L("加载") : L("切换") }
    private var meta: ModelMeta? { console.model(m, on: key) }
    /// 卡片头的小字：版本号和短标签，一行放得下（用户 11-03 定）；内存在信息栏和 ⓘ 里
    private var subtitle: String { [meta?.version ?? "", meta?.label ?? ""].filter { !$0.isEmpty }.joined(separator: " · ") }
    private var need: Int? { n.need_gb?[m] ?? meta?.size_gb }
    /// 鼠标移上去才出现的快捷按钮：没加载的模型不用先展开
    private var quick: Bool { hovering && !open && state == "off" }

    var body: some View {
        VStack(spacing: 0) {
            head
            if open {
                Divider()
                controls.padding(12)
            }
        }
        .background(RoundedRectangle(cornerRadius: 9).fill(Color.primary.opacity(open ? 0.025 : hovering ? 0.05 : 0)))
        .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(Color.primary.opacity(open ? 0.22 : hovering ? 0.2 : 0.12)))
        .onHover { hovering = $0 }
    }

    /// 在线、加载中用小胶囊，一眼能扫到；没加载的只写灰字
    @ViewBuilder
    private var badge: some View {
        if state == "off" {
            Text(L("未加载")).font(.callout).foregroundStyle(.secondary)
        } else {
            let color: Color = state == "loaded" ? .green : .orange
            Text(state == "loaded" ? L("在线") : L("加载中…")).font(.callout.weight(.medium)).foregroundStyle(color)
                .padding(.horizontal, 8).padding(.vertical, 2)
                .background(Capsule().fill(color.opacity(0.15)))
        }
    }

    private var head: some View {
        HStack(spacing: 6) {
            Button(action: toggle) {
                HStack(spacing: 8) {
                    Dot(color: state == "loaded" ? .green : state == "loading" ? .orange : Color.secondary.opacity(0.4), size: 9)
                    Text(title).font(.headline).fixedSize()
                    Text(subtitle.isEmpty ? (meta?.note ?? "") : subtitle).font(.callout).foregroundStyle(.secondary).lineLimit(1)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .layoutPriority(1)
            InfoButton(title: [title, meta?.version ?? ""].joined(separator: " "),
                       items: (meta?.about ?? []) + (need.map { [[L("内存"), L("在 \(console.machine(key)) 上加载约 \($0) GB（不含对话占的上下文）") + (meta?.estimated == true ? L("（估算）") : "")]] } ?? []))
            if !n.isRpc {   // 配对加入的副机：后端还不能经受限钥匙改它的名称和说明，铅笔不出现（点了只会报错）
                Button { editing = true } label: { Image(systemName: "pencil").font(.system(size: 12)).foregroundStyle(.secondary) }
                    .buttonStyle(.borderless)
                    .help(L("编辑这台上这个模型的名称和说明"))
                    .disabled(console.busy || n.reachable == false)
                    .sheet(isPresented: $editing) { NoteSheet(shown: $editing, key: key, m: m).environmentObject(console) }
            }
            Button(action: toggle) {
                HStack(spacing: 8) {
                    Spacer(minLength: 4)
                    if n.options?[m]?.values.contains(where: { $0.pending }) == true {
                        Circle().fill(Color.orange).frame(width: 7, height: 7).help(L("改动待生效"))
                    }
                    badge.opacity(quick ? 0 : 1)
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .onContinuousHover { phase in
            if case .active = phase { NSCursor.pointingHand.set() } else { NSCursor.arrow.set() }
        }
        .overlay(alignment: .trailing) {
            if quick {
                Button { console.load(m, on: key) } label: { Text(verb) }
                    .buttonStyle(.borderedProminent)
                    .disabled(console.busy)
                    .padding(.trailing, 30)
            }
        }
    }

    /// 四排，每张卡片、每种状态都一样高（用户 11-03 定）：操作和开关、运行或加载信息、上下文、dsh 里选哪个
    @ViewBuilder
    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            actions
            pendingNotes
            Grid(alignment: .topLeading, horizontalSpacing: 12, verticalSpacing: 9) {
                InfoRow(label: state == "loaded" ? L("运行") : L("加载")) {
                    Text(infoText).monospacedDigit().fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, minHeight: 34, alignment: .topLeading)
                        .help(L("算法：现在整台最多约 + 它的权重 + 它塞满上下文的 KV 不超过内存安全线，权重也不超过 oMLX 的软水位，才能不卸载别的模型一起跑"))
                }
                InfoRow(label: state == "off" ? L("下次加载") : L("上下文")) { contextRow }
                if console.hasDsh {   // 没有 dsh：这一行不显示，卡片变成三排（9.9 b）
                    InfoRow(label: L("dsh 里选")) {
                        if n.dsh?[m] == false {
                            Text(L("还没有「\(n.dsh_title ?? console.machine(key)) · \(title)」，要先在 dsh 的配置里加上")).foregroundStyle(.orange).lineLimit(1)
                        } else {
                            Text(L("「\(n.dsh_title ?? console.machine(key)) · \(title)」")).lineLimit(1)
                        }
                    }
                }
            }
        }
    }

    /// 第一排：左边两个固定位放操作，右边是两个开关。按钮位置不随状态变
    private var actions: some View {
        HStack(spacing: 8) {
            firstSlot.frame(width: 78, alignment: .leading)
            secondSlot.frame(width: Backend.uiLanguage == "zh" ? 78 : 104, alignment: .leading)   // 英文的 Self-test 比"自检"长，78 放不下会被截成 Self-t…（界面验收 11-22）
            Spacer(minLength: 4)
            optionSlot("mtp")
            optionSlot("ssd")
        }
    }

    @ViewBuilder
    private var firstSlot: some View {
        if state == "loaded" {
            Button { console.unload(m, on: key) } label: { Label(L("卸载"), systemImage: "eject").frame(maxWidth: .infinity) }
                .buttonStyle(.bordered)
                .disabled(console.busy)
        } else if state == "loading" {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(L("加载中")).font(.callout).foregroundStyle(.secondary).fixedSize()
            }
        } else {
            Button { console.load(m, on: key) } label: {
                Label(verb, systemImage: verb == L("切换") ? "arrow.triangle.2.circlepath" : "play.fill").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(console.busy)
            .help(verb == L("切换") ? L("这台放不下一起跑：先卸载 \(others.map { console.title($0, on: key) }.joined(separator: L("、")))，再加载 \(title)") : L("加载 \(title)"))
        }
    }

    /// 放得下、别的模型也在跑时才有两种选择：一起跑（加载）或替换（切换）；其余时候这里是自检
    @ViewBuilder
    private var secondSlot: some View {
        if state == "off", fits, !others.isEmpty {
            Button { console.load(m, on: key, replace: true) } label: { Label(L("切换"), systemImage: "arrow.triangle.2.circlepath").frame(maxWidth: .infinity) }
                .buttonStyle(.bordered)
                .disabled(console.busy)
                .help(L("先卸载 \(others.map { console.title($0, on: key) }.joined(separator: L("、")))，再加载 \(title)"))
        } else {
            Button { console.selftest(m, on: key) } label: { Label(L("自检"), systemImage: "checkmark.circle").frame(maxWidth: .infinity) }
                .buttonStyle(.bordered)
                .disabled(console.busy || state != "loaded")
                .help(state == "loaded" ? L("发一道题，确认模型答得出来") : L("加载后才能自检"))
        }
    }

    /// 一个开关：标签、开关、ⓘ。不适用的灰着占位，左右卡片对得齐
    private func optionSlot(_ opt: String) -> some View {
        let o = n.options?[m]?[opt]
        let label = opt == "mtp" ? "MTP" : L("表放 SSD")
        return HStack(spacing: 4) {
            Text(label).font(.callout).foregroundStyle(o?.mode == "na" || o?.mode == "unsupported" ? .tertiary : .secondary).fixedSize()
            if o?.mode == "locked" { Image(systemName: "lock.fill").font(.caption2).foregroundStyle(.secondary) }
            Toggle("", isOn: Binding(get: { o?.on ?? false }, set: { console.setOption(opt, m, on: key, $0) }))
                .labelsHidden()
                .toggleStyle(.switch)
                .controlSize(.small)
                .disabled(console.busy || o?.mode != "free" || state == "loading")
            InfoButton(title: "\(label) · \(title)", items: meta?.info?[opt] ?? [])
                .opacity(o?.mode == "na" ? 0 : 1)
                .disabled(o?.mode == "na")
        }
        .help(o?.mode == "unsupported" ? L("这台的 oMLX 说这个模型不支持 \(label)") + ((o?.reason).flatMap { $0.isEmpty ? nil : L("：\($0)") } ?? "") :
              o?.mode == "locked" ? (o?.reason == "forced" ? L("放不下，oMLX 自动放 SSD：这台的内存装不下 \(title) 的查询表，开关锁定为开") : L("\(title) 的查询表必须放 SSD，锁定为开")) :
              state == "loaded" ? L("改了要重新加载 \(title) 才生效，会先问你切换还是取消") : L("改了先保存，下次加载时生效"))
        .opacity(o?.mode == "na" ? 0 : 1)   // 模型说明里没声明这个开关：不显示，位置留着让两格对齐（已定第 27 条，草案 9.3）
        .allowsHitTesting(o?.mode != "na")
        .accessibilityHidden(o?.mode == "na")
    }

    /// 开关改了还没生效时多一行；平时不占地方
    @ViewBuilder
    private var pendingNotes: some View {
        ForEach(["mtp", "ssd"], id: \.self) { opt in
            if let o = n.options?[m]?[opt], o.pending {
                Button(L("\(opt == "mtp" ? "MTP" : L("表放 SSD")) 已改为\(o.on ? L("开") : L("关"))，下次加载生效 · 现在切换…")) { console.setOption(opt, m, on: key, o.on) }
                    .buttonStyle(.link)
                    .font(.subheadline)
                    .disabled(console.busy)
            }
        }
    }

    /// 第二排：在线的写运行情况；没加载的写要多少内存、能不能直接加载（用户 11-03 定）
    private var infoText: String {
        if state == "loaded" { return runText }
        let size = need.map { L("需要约 \($0) GB") + (meta?.estimated == true ? L("（估算）") : "") } ?? ""   // 自动识别的陌生模型：内存是估出来的（2b）
        if state == "loading" { return [L("正在加载"), meta?.eta ?? "", size].filter { !$0.isEmpty }.joined(separator: L("，")) }
        let names = others.map { console.title($0, on: key) }.joined(separator: L("、"))
        let plan = others.isEmpty ? L("这台空着，可直接加载") : fits ? L("放得下，和 \(names) 一起跑") : L("放不下，只能切换：先卸载 \(names)")
        return [size, meta?.eta ?? "", plan].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// 上下文：在线的改了当场生效；没加载的存起来，下次加载用。收起时只写档位和推荐，展开才有压缩点和说明；
    /// "推荐"和"已实测"是两回事，实测情况单独一行小字。只有一档（Qwen）就不给下拉框
    @ViewBuilder
    private var contextRow: some View {
        if let c = n.contexts?[m] {
            let now = c.live ?? c.setting
            let note = { (v: Int) in c.notes?[String(v)] ?? "" }
            let long = { (v: Int) in
                ([contextLabel(v), note(v), c.compact?[String(v)] ?? ""].filter { !$0.isEmpty }).joined(separator: " · ")
                    + (c.over?.contains(v) == true ? L("  ⚠️ 超安全线") : "")
            }
            let short = [contextLabel(now), note(now).components(separatedBy: L("，"))[0]].filter { !$0.isEmpty }.joined(separator: " · ")
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    if c.options.count > 1 {
                        Menu {
                            ForEach(c.options, id: \.self) { v in
                                Button { console.setContext(m, on: key, v) } label: {
                                    if v == now { Label(long(v), systemImage: "checkmark") } else { Text(long(v)) }
                                }
                            }
                        } label: { Text(short) }
                        .fixedSize()
                        .disabled(console.busy || state == "loading")
                    } else {
                        Text(long(now))
                    }
                    InfoButton(title: L("上下文 · \(title)"), items: console.model(m, on: key)?.info?["context"] ?? [])
                }
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(([c.options.count > 1 ? c.compact?[String(now)] ?? "" : "", c.marks[String(now)] ?? L("未实测"),
                           c.recommended.flatMap { $0 == now ? nil : L("推荐 \(contextLabel($0))") } ?? ""].filter { !$0.isEmpty }).joined(separator: " · "))
                        .font(.subheadline).foregroundStyle(c.over?.contains(now) == true ? Color.orange : Color.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    // A10：在跑、这一档塞满会超安全线：一键改到不超的最大一档（后端算 over 时已算上同一台别的在线模型）
                    if state == "loaded", let live = c.live, let over = c.over, over.contains(live),
                       let fix = c.options.filter({ $0 < live && !over.contains($0) }).max() {
                        Button(L("改成 \(contextLabel(fix))")) { console.setContext(m, on: key, fix) }
                            .buttonStyle(.bordered).controlSize(.small).fixedSize()
                            .disabled(console.busy)
                            .help(L("塞满会超安全线：改成这一档整台就不超。当场生效，不用重新加载"))
                    }
                }
            }
            .help(!console.hasDsh ? (state == "loaded" ? L("改了当场生效，不用重新加载，对话不中断。模型最多支持 \(contextLabel(c.native))") : L("下次加载 \(title) 时用这一档。模型最多支持 \(contextLabel(c.native))"))
                  : state == "loaded" ? L("改了当场生效，不用重新加载，对话不中断；同时写进 dsh，重开 dsh 后按新额度显示剩余。模型最多支持 \(contextLabel(c.native))")
                                    + (c.max_output.map { L("，单次最多输出 \(contextLabel($0))") } ?? "")
                                    : L("下次加载 \(title) 时用这一档；同时写进 dsh，重开 dsh 后按新额度显示剩余。模型最多支持 \(contextLabel(c.native))"))
        }
    }

    private var runText: String {
        let s = n.stats[m]
        var text = L("处理 \(s?.requests_active ?? 0) 个 · 排队 \(s?.requests_waiting ?? 0) 个")
        if let tps = s?.speed_tps, tps > 0 {
            text += s?.speed_live == true ? L(" · \(Int(tps.rounded())) token/秒（正在生成）") : String(format: L(" · %.1f token/秒（启动以来平均）"), tps)
        }
        return text
    }
}

/// 开关和上下文旁边的 ⓘ：点开看是什么、收益、风险、建议、怎么生效（文案在 clusterctl 里维护，这里只显示）
struct InfoButton: View {
    let title: String
    let items: [[String]]
    @State private var shown = false

    var body: some View {
        Button { shown.toggle() } label: { Image(systemName: "info.circle") }
            .buttonStyle(.borderless)
            .help(L("这一项是什么、有什么收益和风险、什么时候调"))
            .popover(isPresented: $shown, arrowEdge: .bottom) {
                VStack(alignment: .leading, spacing: 10) {
                    Text(title).font(.headline)
                    Grid(alignment: .topLeading, horizontalSpacing: 10, verticalSpacing: 8) {
                        ForEach(items.indices, id: \.self) { i in
                            GridRow {
                                Text(items[i].first ?? "").foregroundStyle(.secondary)
                                Text(items[i].count > 1 ? items[i][1] : "").fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
                .padding(14)
                .frame(width: 380)
            }
    }
}

/// 底部一行：左边机器、密钥（弹窗，管这台 oMLX 的核心设置）；中间最近一次操作的结果；
/// 右边体检、日志、操作记录（都在窗口里展开）。操作记录是这个控制台做过的事，日志是 oMLX 服务自己的记录
struct BottomBar: View {
    @EnvironmentObject var console: Console
    @Binding var showKeys: Bool
    @State private var showMachines = false
    @State private var showHub = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // 放得下就和按钮一行；窄的时候结果单独一行，不被按钮挤没
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) {
                    sheets
                    Spacer(minLength: 8)
                    result.frame(minWidth: 160)
                    Spacer(minLength: 8)
                    panels
                }
                VStack(alignment: .leading, spacing: 6) {
                    if console.lastResult != nil || !console.log.isEmpty { result }
                    HStack(spacing: 8) {
                        sheets
                        Spacer(minLength: 8)
                        panels
                    }
                }
            }
            if let panel = console.panel { panelBody(panel) }
        }
        .padding(.horizontal, 2)
        // 两个弹窗挂在最外层。以前挂在按钮上，而按钮在 ViewThatFits 的两种排法里各有一份：结果文字一长一短，底栏换排法，
        // 开着的弹窗会被拆掉重建，页签回到第一页，填的内容和核对码都丢了（体检 A12）
        .sheet(isPresented: $showMachines) {
            if Backend.isManager { ManagerSetup(shown: $showMachines).environmentObject(console) }
            else { MachineSheet(shown: $showMachines).environmentObject(console) }
        }
        .sheet(isPresented: $showHub) { HubSheet(shown: $showHub).environmentObject(console) }
    }

    @ViewBuilder
    private var sheets: some View {
        if Backend.isManager {
            tool(L("连接"), "link") { showMachines = true }
        } else {
            tool(L("机器"), "desktopcomputer") { showMachines = true }
        }
        tool(L("密钥"), "key") { showKeys = true }
        if Backend.showsHub && (Backend.isManager || nodeOrder.count > 1 || console.hub?.enabled == true) {
            Button { showHub = true } label: {
                Label(console.hub?.applications?.isEmpty == false ? L("主机 ●") : L("主机"), systemImage: "crown")
            }
            .buttonStyle(.bordered)
            .fixedSize()
        }
    }

    @ViewBuilder
    private var panels: some View {
        tool(L("体检"), "stethoscope") { console.doctor() }.disabled(console.running != nil)
        panelButton(.logs, L("日志"))
        panelButton(.records, L("操作记录"))
    }

    @ViewBuilder
    private var result: some View {
        if let r = console.lastResult {
            let text = console.resultTime.isEmpty ? r.text : L("\(r.text) · \(console.resultTime)")
            Text(text).font(.callout).foregroundStyle(console.resultStale ? Color(nsColor: .tertiaryLabelColor) : r.ok ? Color.secondary : Color.red).lineLimit(1)
                .help(console.resultStale ? L("\(text)（之后状态已经变了）") : text)
        } else if let last = console.log.last {
            Text(last.text).font(.callout).foregroundStyle(.secondary).lineLimit(1)
        }
    }

    @ViewBuilder
    private func panelButton(_ which: Console.BottomPanel, _ title: String) -> some View {
        let label = Label(title, systemImage: console.panel == which ? "chevron.up" : "chevron.down")
        if console.panel == which {
            Button { console.togglePanel(which) } label: { label }.buttonStyle(.borderedProminent).fixedSize()
        } else {
            Button { console.togglePanel(which) } label: { label }.buttonStyle(.bordered).fixedSize()
        }
    }

    private func tool(_ title: String, _ icon: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) { Label(title, systemImage: icon) }.buttonStyle(.bordered).fixedSize()
    }

    private func panelBody(_ which: Console.BottomPanel) -> some View {
        let lines = which == .records ? console.log : console.serverLog
        return VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(which == .records ? L("操作记录：这个控制台做过的事") : L("oMLX 日志：服务自己的记录，点开时取一次"))
                    .font(.subheadline).foregroundStyle(.secondary)
                Spacer()
                if which == .records {
                    Button(L("清空")) { console.clearLog() }.disabled(console.log.isEmpty).controlSize(.small)
                } else {
                    Button(console.serverLogLoading ? L("读取中…") : L("刷新")) { console.refreshServerLog() }.disabled(console.serverLogLoading).controlSize(.small)
                }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(lines) { line in
                            Text(line.text)
                                .font(.system(size: 12, design: .monospaced))
                                .foregroundStyle(line.isError ? Color.red : Color.primary)
                                .textSelection(.enabled)
                                .id(line.id)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: lines.last?.id) { if let last = lines.last { proxy.scrollTo(last.id, anchor: .bottom) } }
                .onAppear { if let last = lines.last { proxy.scrollTo(last.id, anchor: .bottom) } }
            }
        }
        .padding(10)
        .frame(height: Layout.panel - 8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.primary.opacity(0.04)))
    }
}

/// 一张表管所有机器的子密钥（用户 11-03 定）：新建默认建到每台，各台不一致时标出来
/// 卡片"编辑"（2b，已定第 27 条 c）：改这台上这个模型的名称、一句话说明、推荐档位，存在模型所在的那台；
/// 每项能"恢复默认"，回到出厂说明。存好后刷新一次，卡片马上变
struct NoteSheet: View {
    @EnvironmentObject var console: Console
    @Binding var shown: Bool
    let key: String
    let m: String
    @State private var name = ""
    @State private var note = ""
    @State private var recommended = 0
    @State private var start: (String, String, Int) = ("", "", 0)
    @State private var problem: String?
    @State private var saving = false

    private var options: [Int] { console.node(key)?.contexts?[m]?.options ?? [] }

    /// 存好了才关窗；没存成窗口留着、填的内容还在，原因写在窗口里。以前先关窗再交后端校验：说明清空了、名称太长，
    /// 或者正好有别的操作在跑，整次作废，改对的那几项也丢了（体检 A13）
    private func submit(_ label: String, _ args: [String], input: String? = nil) {
        guard console.running == nil else { problem = L("有别的操作正在进行，等它做完再点。填的内容还在。"); return }
        problem = nil
        saving = true
        console.perform(label, args, summary: true, input: input, onDone: { ok in
            saving = false
            if ok { shown = false; console.refreshNow(); return }
            let why = console.lastResult.flatMap { $0.ok ? nil : $0.text.replacingOccurrences(of: L("操作未完成："), with: "") }
            problem = why ?? L("没有存成，原因见操作记录。")
        })
    }

    private func reset(_ field: String) { submit(L("恢复默认"), ["note", key, m, "reset", field]) }

    /// 字数按后端的算法数（clean_text：名称最多 40 个字，说明最多 120 个字，都不能空）
    private func tooLong(_ text: String, _ most: Int) -> Bool { text.trimmingCharacters(in: .whitespacesAndNewlines).unicodeScalars.count > most }

    private func save() {
        var changes: [String: Any] = [:]
        if name != start.0 { changes["title"] = name }
        if note != start.1 { changes["note"] = note }
        if recommended != start.2 && recommended > 0 { changes["recommended"] = recommended }
        guard !changes.isEmpty else { shown = false; return }
        if changes["title"] != nil, tooLong(name, 40) { problem = L("名称太长了：最多 40 个字。"); return }
        if changes["note"] != nil, note.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { problem = L("一句话说明不能是空的；想回到出厂说明就点它右边的「恢复默认」。"); return }
        if changes["note"] != nil, tooLong(note, 120) { problem = L("一句话说明太长了：最多 120 个字。"); return }
        guard let data = try? JSONSerialization.data(withJSONObject: changes), let text = String(data: data, encoding: .utf8) else { return }
        submit(L("保存说明"), ["note", key, m, "set"], input: text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("编辑 \(console.machine(key)) 上的 \(console.title(m, on: key))")).font(.title2.bold())
            Text(L("改的是这台的说法，存在 \(console.machine(key)) 上。\"恢复默认\"回到出厂说明。")).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 10) {
                GridRow {
                    Text(L("名称"))
                    TextField("", text: $name).frame(width: 260)
                    Button(L("恢复默认")) { reset("title") }
                }
                GridRow {
                    Text(L("一句话说明"))
                    TextField("", text: $note).frame(width: 260)
                    Button(L("恢复默认")) { reset("note") }
                }
                if !options.isEmpty {
                    GridRow {
                        Text(L("推荐档位"))
                        Picker("", selection: $recommended) {
                            if recommended == 0 { Text(L("没有")).tag(0) }
                            ForEach(options, id: \.self) { Text(contextLabel($0)).tag($0) }
                        }
                        .labelsHidden().frame(width: 120, alignment: .leading)
                        Button(L("恢复默认")) { reset("recommended") }
                    }
                }
            }
            .disabled(saving)
            if let problem { Text(problem).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }
            HStack {
                Button(L("全部恢复默认")) { reset("all") }.disabled(saving)
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button(L("取消")) { shown = false }.keyboardShortcut(.cancelAction)
                Button(L("保存")) { save() }.keyboardShortcut(.defaultAction)
                    .disabled(saving || name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 520)
        .onAppear {
            start = (console.title(m, on: key), console.model(m, on: key)?.note ?? "", console.node(key)?.contexts?[m]?.recommended ?? 0)
            (name, note, recommended) = start
        }
    }
}

/// 0.5.1 B1（preview.2 计划 2.1）：点"密钥"看到的是"连接客户端"——客户端怎么填（地址、模型名、密钥）、子密钥（显示、复制、停用、删除）、主密钥。
/// 地址是列表（0.6 报告 R1）：0.5.1 只有本机一行，第二行灰着留给局域网
struct KeySheet: View {
    @EnvironmentObject var console: Console
    @Binding var shown: Bool
    @State private var newName = ""
    @State private var coverage: KeyCoverage?
    @State private var loading = false
    @State private var revealed: [String: String] = [:]   // 正在显示完整密钥的那几行（"" 是主密钥）；10 秒后收起，不存
    @State private var copied = ""                         // 刚复制了哪一项，那个按钮短暂写"已复制"

    private var localUp: Bool { console.localKey.flatMap { console.node($0)?.up } == true }
    private var order: [String] { coverage?.order ?? nodeOrder }
    private var many: Bool { order.count > 1 }
    private var canDisable: Bool { console.status?.keys?.can_disable == true }
    private var blocked: Bool { console.engine?.keyBlocked == true }
    private var rows: [CoveredKey] {
        coverage?.sub_keys ?? (console.status?.keys?.sub_keys ?? []).map {
            CoveredKey(name: $0.name, created_at: $0.created_at, hint: $0.hint, disabled: $0.disabled, on: console.localKey.map { [$0] } ?? [])
        }
    }
    private var urls: [ClientURL] {
        console.engine?.client_urls ?? [ClientURL(id: "local", name: L("本机"), url: "http://127.0.0.1:\(console.clientPort)/v1")]
    }
    /// 这台有的模型：(客户端里填的名字, 现在在不在线)
    private var modelNames: [(id: String, loaded: Bool)] {
        guard let key = console.localKey, let n = console.node(key) else { return [] }
        return console.models(on: key).compactMap { m in console.model(m, on: key).map { (id: $0.id, loaded: n.models[m] == "loaded") } }
    }

    private func reload() {
        loading = true
        DispatchQueue.global().async {
            let decoded = try? JSONDecoder().decode(KeyCoverage.self, from: Data(Backend.run(["subkey", "where"]).out.utf8))
            DispatchQueue.main.async {
                coverage = decoded ?? coverage
                loading = false
            }
        }
    }

    private func onHere(_ k: CoveredKey) -> Bool { console.localKey.map { k.on.contains($0) } ?? false }

    /// 地址、模型名不是秘密：直接放剪贴板，按钮上写一下"已复制"
    private func copyPlain(_ text: String, mark: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        copied = mark
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { if copied == mark { copied = "" } }
    }

    /// 👁：现从 oMLX 读完整密钥，显示 10 秒，同时复制（30 秒后清空剪贴板）；再点一下收起。name 为空是主密钥
    private func toggleReveal(_ name: String) {
        if revealed[name] != nil { revealed[name] = nil; return }
        DispatchQueue.global().async {
            struct Reply: Decodable { let ok: Bool; let key: String }
            let r = Backend.run(name.isEmpty ? ["key", "reveal"] : ["subkey", "reveal", name])
            let reply = lastJSON(r.out, as: Reply.self)
            DispatchQueue.main.async {
                guard let value = reply?.key, reply?.ok == true else { console.lastResult = (false, failureText(r)); return }
                revealed[name] = value
                Backend.copySecret(value)
                DispatchQueue.main.asyncAfter(deadline: .now() + 10) { if revealed[name] == value { revealed[name] = nil } }
            }
        }
    }

    private func secretText(_ name: String, hint: String) -> some View {
        HStack(spacing: 6) {
            if let value = revealed[name] {
                Text(verbatim: value).font(.system(.callout, design: .monospaced)).textSelection(.enabled).lineLimit(2).fixedSize(horizontal: false, vertical: true)
            } else {
                Text(verbatim: "●●●●●●●●" + hint.replacingOccurrences(of: "…", with: "")).font(.system(.body, design: .monospaced))
            }
            if !Backend.isManager {
                Button { toggleReveal(name) } label: { Image(systemName: revealed[name] == nil ? "eye" : "eye.slash") }
                    .buttonStyle(.borderless)
                    .disabled(blocked)
                    .help(revealed[name] == nil ? L("显示完整密钥 10 秒，同时复制到剪贴板（30 秒后自动清空）") : L("收起"))
            }
        }
    }

    private var howTo: some View {
        GroupBox {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 12, verticalSpacing: 7) {
                GridRow {
                    Text(L("在聊天、写代码的软件里选「OpenAI 兼容」，填这三样：")).gridCellColumns(3)
                }
                ForEach(urls) { u in
                    GridRow {
                        Text(u.id == urls.first?.id ? L("地址") : "").foregroundStyle(.secondary)
                        HStack(spacing: 8) {
                            Tag(text: u.name)
                            Text(verbatim: u.url).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                        }
                        HStack(spacing: 6) {
                            Button(copied == u.url ? L("已复制") : L("复制")) { copyPlain(u.url, mark: u.url) }
                            Button(copied == u.url + "#root" ? L("已复制") : L("复制根地址")) {
                                copyPlain(u.url.hasSuffix("/v1") ? String(u.url.dropLast(3)) : u.url, mark: u.url + "#root")
                            }
                            .help(L("不带 /v1 的地址。Chatbox、Cherry Studio 这类软件要填这个"))
                        }
                        .controlSize(.small)
                    }
                }
                if !Backend.isManager {
                    GridRow {
                        Text("")
                        HStack(spacing: 8) {
                            Tag(text: L("局域网"))
                            Text(L("0.6 起支持")).foregroundStyle(.tertiary)
                        }
                        Text("")
                    }
                    .help(L("上面的地址只在这台 Mac 上能用。让别的电脑、手机连这台，在 0.6 版支持"))
                }
                if modelNames.isEmpty {
                    GridRow {
                        Text(L("模型名")).foregroundStyle(.secondary)
                        Text(L("这台还没有模型。")).foregroundStyle(.secondary)
                        Text("")
                    }
                }
                ForEach(Array(modelNames.enumerated()), id: \.offset) { pair in
                    GridRow {
                        Text(pair.offset == 0 ? L("模型名") : "").foregroundStyle(.secondary)
                        HStack(spacing: 8) {
                            Text(verbatim: pair.element.id).font(.system(.body, design: .monospaced)).textSelection(.enabled)
                                .foregroundStyle(pair.element.loaded ? Color.primary : Color.secondary).lineLimit(1).truncationMode(.middle)
                                .layoutPriority(1)   // 模型名要让人看全（照着填的）；挤的时候先缩后面那句灰字（界面验收 11-22：英文下模型名被截）
                            if !pair.element.loaded { Text(L("加载后可用")).font(.callout).foregroundStyle(.tertiary).lineLimit(1) }
                        }
                        Button(copied == pair.element.id ? L("已复制") : L("复制")) { copyPlain(pair.element.id, mark: pair.element.id) }.controlSize(.small)
                    }
                }
                GridRow {
                    Text(L("密钥")).foregroundStyle(.secondary)
                    Text(L("用下面的子密钥，每个客户端一把；不要填主密钥。")).fixedSize(horizontal: false, vertical: true).gridCellColumns(2)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(4)
        } label: { Label(L("客户端怎么填"), systemImage: "link") }
    }

    private var subKeys: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(many ? L("子密钥（客户端用；新建会同时建到\(console.every)）") : L("子密钥（客户端用）")).font(.headline)
                Spacer()
                if loading { ProgressView().controlSize(.small) }
            }
            if rows.isEmpty {
                Text(L("还没有子密钥。")).foregroundStyle(.secondary)
            } else {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 8) {
                    GridRow {
                        Text(L("名字")).foregroundStyle(.secondary)
                        Text(L("密钥")).foregroundStyle(.secondary)
                        Text(L("建于")).foregroundStyle(.secondary)
                        Text(L("状态")).foregroundStyle(.secondary)
                        if many { Text(L("在哪几台")).foregroundStyle(.secondary) }
                        Text("")
                    }
                    ForEach(rows) { k in
                        let off = k.disabled == true
                        GridRow {
                            Text(k.name.isEmpty ? L("（未命名）") : k.name).bold().foregroundStyle(off ? Color.secondary : Color.primary)
                            if k.name.isEmpty || !onHere(k) {
                                Text(verbatim: "●●●●●●●●" + k.hint.replacingOccurrences(of: "…", with: "")).font(.system(.body, design: .monospaced))
                            } else {
                                secretText(k.name, hint: k.hint)
                            }
                            Text(String(k.created_at.prefix(10))).monospacedDigit()
                            Text(off ? L("已停用") : L("能用")).foregroundStyle(off ? Color.orange : Color.green).fixedSize()
                            if many {
                                HStack(spacing: 8) {
                                    ForEach(order, id: \.self) { node in
                                        let unknown = coverage?.unreachable.contains(node) == true
                                        let has = k.on.contains(node)
                                        Label(console.machine(node), systemImage: unknown ? "questionmark.circle" : has ? "checkmark.circle.fill" : "xmark.circle.fill")
                                            .font(.callout)
                                            .foregroundStyle(unknown ? Color.secondary : has ? Color.green : Color.orange)
                                            .fixedSize()
                                            .help(unknown ? L("\(console.machine(node)) 现在连不上，没查到") : has ? L("这把在 \(console.machine(node)) 上能用") : L("这把在 \(console.machine(node)) 上没有，那台调用会被拒绝"))
                                    }
                                }
                            }
                            HStack(spacing: 6) {
                                if !Backend.isManager {
                                    Button(L("复制")) { console.copySubKey(k.name) }
                                        .disabled(console.running != nil || k.name.isEmpty || !onHere(k))
                                }
                                if off {
                                    Button(L("启用")) { console.setSubKey(k.name, disabled: false) }
                                        .disabled(console.running != nil || k.name.isEmpty || !onHere(k))
                                } else {
                                    Button(L("停用")) { console.setSubKey(k.name, disabled: true) }
                                        .disabled(!canDisable || console.running != nil || k.name.isEmpty || !onHere(k))
                                        .help(canDisable ? L("经入口的调用立刻被拒；密钥还留着，随时能启用") : L("入口关着或不在这台上，停用不起作用。要停掉一把，只能删除它"))
                                }
                                Button(L("删除…")) { console.revokeSubKey(k.name) }
                                    .disabled(!localUp || console.running != nil || k.name.isEmpty || !onHere(k))
                            }
                            .controlSize(.small)
                            .help(onHere(k) ? "" : L("这把不在本机上，到它所在机器的管理页处理"))
                        }
                    }
                }
            }
            if let missing = coverage?.unreachable, !missing.isEmpty {
                Text(L("\(missing.map(console.machine).joined(separator: L("、"))) 现在连不上，那一栏没查到。")).font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                TextField(L("新子密钥的名字：一台设备一个软件一把，比如 macbook-dsh"), text: $newName)
                    .textFieldStyle(.roundedBorder)
                Button(L("新建并复制")) {
                    console.createSubKey(newName.trimmingCharacters(in: .whitespaces))
                    newName = ""
                }
                .disabled(!localUp || console.running != nil || newName.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
    }

    private var mainKey: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L("主密钥（管理用，不要填进客户端）")).font(.headline)
            if !Backend.isManager {
                HStack(spacing: 10) {
                    secretText("", hint: "●●●●")
                    Button(L("复制（30 秒后清空剪贴板）")) { console.copyMainKey() }
                        .disabled(console.running != nil || blocked)
                    Spacer()
                    Button(L("重置主密钥…"), role: .destructive) { console.resetMainKey() }
                        .disabled(!localUp || console.running != nil)
                }
            }
            Text(console.status?.keys?.note ?? "").font(.callout).fixedSize(horizontal: false, vertical: true).foregroundStyle(.secondary)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("连接客户端")).font(.title2.bold())
            if let e = console.engine, e.keyBlocked {
                Text(L("这台的 oMLX 是官方版，还没允许控制台读它的主密钥：子密钥看不到，也建不了。到设置的 oMLX 页允许。"))
                    .fixedSize(horizontal: false, vertical: true).foregroundStyle(.orange)
            }
            howTo
            Divider()
            subKeys
            Divider()
            mainKey
            Divider()
            HStack {
                if let r = console.lastResult { Text(r.text).foregroundStyle(r.ok ? .green : .red).lineLimit(2) }
                Spacer()
                Button(L("完成")) { shown = false }.keyboardShortcut(.defaultAction)
            }
            .controlSize(.large)
        }
        .padding(22)
        .frame(width: many ? 820 : 720)
        .onAppear { reload() }
        .onChange(of: console.running) { if console.running == nil { reload() } }
    }
}

/// 管理版连不上主机时：主机可能没解锁，也可能崩了
struct ManagerRescue: View {
    @EnvironmentObject var console: Console

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(console.hubOff ? L("主机已关机") : L("连不上主机")).font(.headline)
            Text(console.hubOff ? L("要再开机，到机器上按电源键，开机后在这里解锁。主机坏了才让名单里的另一台接管，要最高密码。")
                                : L("主机刚开机就先解锁；主机坏了，就让名单里的另一台接管，要最高密码。开机只能到机器上按电源键。"))
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            ForEach(console.knownHosts) { host in
                HStack {
                    Text(host.name).bold()
                    Text(host.ip).font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Button(L("解锁…")) { console.unlockDirect(host) }.help(L("打开终端，把这台的 macOS 密码直接输给系统"))
                    Button(L("连它")) { Backend.hubAlias = "lmc-" + host.name; console.refreshNow() }.help(L("这台就是现在的主机时用"))
                    Button(L("强制接管…"), role: .destructive) { console.takeover(on: host) }.disabled(console.running != nil)
                }
            }
            if console.knownHosts.isEmpty { Text(L("还没连接过任何机器，点左下角的连接。")).foregroundStyle(.secondary) }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.orange.opacity(0.15)))
    }
}

// MARK: - 管理版：连接主机。这边生成钥匙，配对串拿到主机的控制台由人点允许（D1）

struct ManagerSetup: View {
    @EnvironmentObject var console: Console
    @Binding var shown: Bool
    @State private var name = "studio"
    @State private var address = ""
    @State private var user = NSUserName()
    @State private var pair = ""
    @State private var code = ""
    @State private var failure = ""

    private func tool(_ path: String, _ args: [String]) -> (Int32, String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: path)
        p.arguments = args
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch { return (-1, "") }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    /// 和 clusterctl 的 machine add 同样的做法：钥匙、主机指纹、连接配置都在 ~/.ssh/lmc，不动用户自己的 ~/.ssh/config
    private func prepare() {
        failure = ""
        let dir = NSHomeDirectory() + "/.ssh/lmc"
        let fm = FileManager.default
        try? fm.createDirectory(atPath: dir, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let key = dir + "/" + name
        let hostName = Host.current().localizedName ?? L("管理控制台")
        if !fm.fileExists(atPath: key) {
            guard tool("/usr/bin/ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-C", "lmc-manager", "-f", key]).0 == 0 else { failure = L("生成钥匙失败。"); return }
        }
        let scan = tool("/usr/bin/ssh-keyscan", ["-T", "5", "-t", "ed25519", address]).1
        guard let line = scan.split(separator: "\n").first(where: { $0.contains("ssh-ed25519") }) else { failure = L("连不上 \(address)，确认主机开着、打开了远程登录。"); return }
        let hostKey = line.split(separator: " ").dropFirst().joined(separator: " ")
        let known = dir + "/known_hosts"
        let old = ((try? String(contentsOfFile: known, encoding: .utf8)) ?? "").split(separator: "\n").filter { !$0.hasPrefix(address + " ") }.map(String.init)
        try? ((old + ["\(address) \(hostKey)"]).joined(separator: "\n") + "\n").write(toFile: known, atomically: true, encoding: .utf8)
        let block = """
        Host lmc-\(name)
          HostName \(address)
          User \(user)
          IdentityFile "\(key)"
          IdentitiesOnly yes
          UserKnownHostsFile "\(known)"
          StrictHostKeyChecking yes
          BatchMode yes
          ConnectTimeout 3

        """
        let config = dir + "/config"
        let current = (try? String(contentsOfFile: config, encoding: .utf8)) ?? "# ModelTower（管理版）生成\n"
        if !current.contains("Host lmc-\(name)\n") { try? (current + block).write(toFile: config, atomically: true, encoding: .utf8) }
        Backend.hubAlias = "lmc-" + name
        let pub = ((try? String(contentsOfFile: key + ".pub", encoding: .utf8)) ?? "").split(separator: " ").prefix(2).joined(separator: " ")
        let id = "manager-" + String(format: "%06x", Int.random(in: 0..<0xffffff))
        let payload = (try? JSONSerialization.data(withJSONObject: ["id": id, "title": hostName, "key": pub])) ?? Data()
        pair = "lmc1." + payload.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
        code = Chip.pairCode(pub: pub, hostKey: hostKey)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("机器")).font(.title2.bold())
            if !nodeOrder.isEmpty {
                DisclosureGroup(L("卡片和模型的顺序（只影响这个控制台）")) { OrderList().environmentObject(console).padding(.top, 6) }
                Divider()
            }
            Text(L("连接主机")).font(.headline)
            Text(L("管理版只是遥控器，不跑模型。在这里生成一把钥匙，把配对串拿到主机的控制台：机器 → 粘贴配对串 → 核对 6 位码 → 允许。")).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                GridRow { Text(L("主机名字")).foregroundStyle(.secondary); TextField("studio", text: $name).textFieldStyle(.roundedBorder) }
                GridRow { Text(L("地址")).foregroundStyle(.secondary); TextField(L("192.168.1.20 或 my-mac.local"), text: $address).textFieldStyle(.roundedBorder) }
                GridRow { Text(L("账号")).foregroundStyle(.secondary); TextField(L("主机上的登录账号"), text: $user).textFieldStyle(.roundedBorder) }
            }
            HStack {
                Button(L("生成配对串")) { prepare() }
                    .buttonStyle(.borderedProminent)
                    .disabled(address.isEmpty || user.isEmpty || name.isEmpty)
                if !failure.isEmpty { Text(failure).foregroundStyle(.red) }
            }
            if !pair.isEmpty {
                Divider()
                Text(L("核对码 \(code)：要和主机上显示的一样才点允许。")).bold()
                Text(pair).font(.system(size: 11, design: .monospaced)).textSelection(.enabled).lineLimit(3)
                Button(L("复制配对串")) {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(pair, forType: .string)
                }
            }
            Divider()
            HStack {
                Spacer()
                Button(L("完成")) { shown = false; console.refreshNow() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(22)
        .frame(width: 520)
    }
}

// MARK: - 主机角色：谁是主机、移交、申请、强制接管、最高密码。动信任的只在主机本机显示

struct HubSheet: View {
    @EnvironmentObject var console: Console
    @Binding var shown: Bool
    @State private var target = ""
    @State private var picked: Set<String> = []
    @State private var line = ""

    private var hub: HubView? { console.hub }
    private var isHub: Bool { hub?.is_hub == true }
    private var others: [String] { (hub?.candidates ?? []).filter { $0 != hub?.hub } }
    private var running: Bool { console.running != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(L("主机")).font(.title2.bold())
            if hub?.enabled != true { notEnabled } else { enabled }
            Divider()
            HStack {
                if let r = console.lastResult { Text(r.text).foregroundStyle(r.ok ? .green : .red).lineLimit(2) }
                Spacer()
                Button(L("完成")) { shown = false }.keyboardShortcut(.defaultAction)
            }
            .controlSize(.large)
        }
        .padding(22)
        .frame(width: 560)
        .onAppear {
            console.checkHub()
            picked = Set(nodeOrder.filter { console.node($0)?.local != true })
        }
    }

    @ViewBuilder
    private var notEnabled: some View {
        Text(L("现在各台各自管理。启用后，一台主机总控，别的机器只听它的；主机可以移交，主机崩了名单里的机器能凭最高密码接管。启用不影响正在跑的模型。"))
            .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        if Backend.isManager {
            Text(L("要在主机本机上启用。")).foregroundStyle(.orange)
        } else {
            Text(L("可当主机的机器")).font(.headline)
            ForEach(nodeOrder.filter { console.node($0)?.local != true }, id: \.self) { name in
                Toggle(console.machine(name), isOn: Binding(get: { picked.contains(name) }, set: { if $0 { picked.insert(name) } else { picked.remove(name) } }))
            }
            if hub?.password_set == true {
                Button(L("启用主机模式…")) { console.hubEnable(candidates: picked.sorted()) }
                    .buttonStyle(.borderedProminent)
                    .disabled(running)
            } else {
                HStack {
                    Text(L("先设最高密码。")).foregroundStyle(.orange)
                    SettingsButton(title: L("打开设置"), tab: .security)
                }
            }
        }
    }

    @ViewBuilder
    private var enabled: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
            GridRow { Text(L("现在的主机")).foregroundStyle(.secondary); Text(console.hubTitle(hub?.hub)).bold() }
            GridRow { Text(L("可当主机")).foregroundStyle(.secondary); Text((hub?.candidates ?? []).map(console.hubTitle).joined(separator: L("、"))) }
            if !Backend.isManager { GridRow { Text(L("本机")).foregroundStyle(.secondary); Text(isHub ? L("是主机") : L("是副机，归主机管")) } }
        }
        if hub?.locked == true { Text(L("最高密码连错太多次，暂时锁定了。")).foregroundStyle(.red) }

        if isHub || Backend.isManager {
            if let apply = hub?.applications, !apply.isEmpty {
                Divider()
                Text(L("申请当主机")).font(.headline)
                ForEach(apply, id: \.self) { name in
                    HStack {
                        Text(console.hubTitle(name))
                        Spacer()
                        Button(L("批准…")) { console.hubHandover(to: name, approving: true) }.disabled(running)
                        Button(L("拒绝")) { console.hubReject(name) }.disabled(running)
                    }
                }
            }
            Divider()
            HStack {
                Picker(L("移交给"), selection: $target) {
                    Text(L("选一台")).tag("")
                    ForEach(others, id: \.self) { Text(console.hubTitle($0)).tag($0) }
                }
                .fixedSize()
                Button(L("移交…")) { console.hubHandover(to: target) }.disabled(running || target.isEmpty)
            }
            if Backend.isManager {
                Text(L("远程只能在名单内移交；授权、名单、最高密码只能在主机本机改。")).font(.callout).foregroundStyle(.secondary)
            }
        }
        if isHub && !Backend.isManager {
            Divider()
            Text(L("只能在主机本机做")).font(.headline)
            HStack {
                Button(L("通知各台")) { console.hubJoin() }.disabled(running).help(L("把现在的主机和加密的钥匙发给各台；加了新机器的授权行之后点一次"))
                SettingsButton(title: L("最高密码在设置里改"), tab: .security)
                Spacer()
            }
            ForEach((hub?.machines ?? [:]).keys.sorted().filter { $0 != hub?.me }, id: \.self) { name in
                Toggle(console.hubTitle(name) + L(" 可以当主机"), isOn: Binding(
                    get: { hub?.candidates?.contains(name) == true },
                    set: { on in
                        let now = Set(hub?.candidates ?? [])
                        console.hubSetCandidates((on ? now.union([name]) : now.subtracting([name])).sorted())
                    }))
                .disabled(running)
            }
        }
        if !isHub && !Backend.isManager {
            Divider()
            HStack {
                Button(L("申请当主机")) { console.hubApply() }.disabled(running).help(L("主机批准后才换；10 分钟没人批就失效"))
                if hub?.candidates?.contains(hub?.me ?? "") == true {
                    Button(L("强制接管…"), role: .destructive) { console.hubTakeover() }.disabled(running).help(L("主机崩了才用，要最高密码"))
                }
                Spacer()
            }
        }
    }
}

// MARK: - 机器：卡片顺序和名字、添加机器、这台的准备和授权（用户 11-04 定）

/// 上下两个小箭头
struct MoveButtons: View {
    let up: Bool
    let down: Bool
    let move: (Int) -> Void
    var body: some View {
        HStack(spacing: 2) {
            Button { move(-1) } label: { Image(systemName: "chevron.up") }.disabled(!up).help(L("往前排"))
            Button { move(1) } label: { Image(systemName: "chevron.down") }.disabled(!down).help(L("往后排"))
        }
        .buttonStyle(.borderless)
    }
}

/// 卡片和模型的顺序。每个控制台记自己的，主机上还能改名
struct OrderList: View {
    @EnvironmentObject var console: Console
    @State private var opened: Set<String> = []
    @State private var names: [String: String] = [:]
    @State private var editing: String?          // 正在改地址的那台
    @State private var drafts: [AddressRow] = []
    @State private var ports: [PortRow] = []
    @State private var portsNote = ""

    /// 主机模式下地址由主机管，暂时不能在这里改（第 4 期）；管理版不管机器表
    private var canEditAddresses: Bool { !Backend.isManager && console.hub?.enabled != true }
    /// 连接方式的选项从后端拿：机器表里存的是中文，界面不写中文数据
    private var links: [[String]] { (console.status?.links ?? []).filter { $0.count == 2 } }
    private func link(_ i: Int) -> String { links.indices.contains(i) ? links[i][0] : "" }

    private func toggleAddresses(_ key: String) {
        if editing == key { editing = nil; return }
        editing = key
        drafts = console.node(key)?.addresses ?? []
        if drafts.isEmpty { drafts = [AddressRow(ip: "", interface: "en0", link: link(0))] }
        ports = []
        portsNote = ""
    }

    private func field(_ i: Int, _ path: WritableKeyPath<AddressRow, String>) -> Binding<String> {
        Binding(get: { drafts.indices.contains(i) ? drafts[i][keyPath: path] : "" }, set: { if drafts.indices.contains(i) { drafts[i][keyPath: path] = $0 } })
    }

    private func loadPorts(_ key: String) {
        portsNote = L("正在查…")
        DispatchQueue.global().async {
            let r = Backend.run(["machine", "ports", key])
            let list = try? JSONDecoder().decode(PortList.self, from: Data(r.out.utf8))
            DispatchQueue.main.async {
                ports = list?.ports ?? []
                portsNote = list == nil ? r.err.replacingOccurrences(of: "✕ ", with: "").trimmingCharacters(in: .whitespacesAndNewlines) : ports.isEmpty ? L("没查到网口") : ""
            }
        }
    }

    private func saveAddresses(_ key: String) {
        let rows = drafts.map { AddressRow(ip: $0.ip.trimmingCharacters(in: .whitespaces), interface: $0.interface.trimmingCharacters(in: .whitespaces), link: $0.link) }
        guard let data = try? JSONEncoder().encode(rows), let json = String(data: data, encoding: .utf8) else { return }
        console.perform(L("改 \(console.machine(key)) 的地址"), ["machine", "addresses", key, "set"], summary: true, input: json, onDone: { ok in if ok { editing = nil } })
    }

    private func addressEditor(_ key: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(L("网线在前，连不上第一个就用下一个，最多 4 个。建议在路由器里给这台绑定固定地址，做法见使用指南。"))
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            ForEach(Array(drafts.enumerated()), id: \.offset) { i, row in
                HStack(spacing: 8) {
                    MoveButtons(up: i > 0, down: i < drafts.count - 1) { delta in if drafts.indices.contains(i + delta) { drafts.swapAt(i, i + delta) } }
                    TextField(L("地址，如 192.168.1.20"), text: field(i, \.ip)).frame(width: 150)
                    TextField(L("网口"), text: field(i, \.interface)).frame(width: 64).help(L("比如 en0；不知道就点下面的“查网口”"))
                    Picker("", selection: field(i, \.link)) {
                        ForEach(links.contains { $0[0] == row.link } ? links : links + [[row.link, row.link]], id: \.self) { Text($0[1]).tag($0[0]) }
                    }
                    .labelsHidden().fixedSize()
                    Button { if drafts.indices.contains(i) { drafts.remove(at: i) } } label: { Image(systemName: "minus.circle") }
                        .buttonStyle(.borderless).disabled(drafts.count < 2).help(L("删掉这个地址"))
                    if console.node(key)?.address == row.ip && !row.ip.isEmpty { Tag(text: L("在用")) }
                }
            }
            HStack(spacing: 8) {
                Button(L("加一个地址")) { drafts.append(AddressRow(ip: "", interface: "en1", link: link(1))) }.disabled(drafts.count >= 4)
                Button(L("查网口")) { loadPorts(key) }.help(L("列出这台的网线、Wi-Fi 网口和它们现在的地址"))
                Spacer()
                Button(L("取消")) { editing = nil }
                Button(L("保存")) { saveAddresses(key) }
                    .buttonStyle(.borderedProminent)
                    .disabled(console.running != nil || drafts.contains { $0.ip.trimmingCharacters(in: .whitespaces).isEmpty || $0.interface.trimmingCharacters(in: .whitespaces).isEmpty })
                    .help(console.node(key)?.local == true ? L("保存到这台") : L("保存到这台，并同步给 \(console.machine(key))（同步前都先备份）"))
            }
            if !portsNote.isEmpty { Text(portsNote).font(.callout).foregroundStyle(.secondary) }
            ForEach(ports, id: \.self) { p in
                Text(L("\(p.interface) · \(p.port) · \(p.ip.isEmpty ? L("没有地址") : p.ip)")).font(.callout).monospacedDigit().foregroundStyle(.secondary)
            }
        }
        .textFieldStyle(.roundedBorder)
        .padding(.leading, 26)
    }

    private var canRename: Bool { !Backend.isManager && console.localIsHub && console.hub?.is_hub != false }

    private func rename(_ key: String) {
        let title = (names[key] ?? "").trimmingCharacters(in: .whitespaces)
        guard !title.isEmpty, title != console.machine(key) else { return }
        console.perform(L("改名为 \(title)"), ["machine", "rename", key, title], summary: true, onDone: { _ in names[key] = nil })
    }

    private func row(_ key: String, _ index: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                MoveButtons(up: index > 0, down: index < nodeOrder.count - 1) { console.moveMachine(key, by: $0) }
                if canRename {
                    TextField(L("名称"), text: Binding(get: { names[key] ?? console.machine(key) }, set: { names[key] = $0 }))
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 150)
                        .onSubmit { rename(key) }
                    if let draft = names[key], draft != console.machine(key) {
                        Button(L("改名")) { rename(key) }.disabled(console.running != nil)
                    }
                } else {
                    Text(console.machine(key)).fontWeight(.medium)
                }
                Text(console.node(key)?.role ?? "").foregroundStyle(.secondary)
                if console.node(key)?.local == true && !Backend.isManager { Tag(text: L("本机")) }
                Spacer()
                if canEditAddresses {
                    Button { toggleAddresses(key) } label: {
                        Label(L("地址"), systemImage: editing == key ? "chevron.up" : "chevron.down")
                    }
                    .buttonStyle(.borderless)
                    .help((console.node(key)?.addresses ?? []).map { $0.ip }.joined(separator: " / "))
                }
                Button { if opened.contains(key) { opened.remove(key) } else { opened.insert(key) } } label: {
                    Label(L("模型顺序"), systemImage: opened.contains(key) ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(.borderless)
            }
            if editing == key { addressEditor(key) }
            if opened.contains(key) {
                let list = console.models(on: key)
                ForEach(Array(list.enumerated()), id: \.element) { i, m in
                    HStack(spacing: 8) {
                        MoveButtons(up: i > 0, down: i < list.count - 1) { console.moveModel(m, on: key, by: $0) }
                        Text(console.title(m, on: key))
                        Text(console.model(m, on: key)?.version ?? "").foregroundStyle(.secondary)
                        Spacer()
                    }
                    .padding(.leading, 26)
                }
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(nodeOrder.enumerated()), id: \.element) { i, key in row(key, i) }
            HStack {
                Text(L("默认本机最前，主机其次，其余按添加顺序；模型新的在前。") + (canRename && console.hasDsh ? L("改名后 dsh 里显示的名字不变。") : ""))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Spacer()
                Button(L("恢复默认顺序")) { console.resetOrder() }
            }
        }
    }
}

/// 配好后自检的结果（第 4 期，草案 11.4）：六项各一行，不通的带一句怎么办
struct MachineCheckList: View {
    let result: MachineCheck

    private func icon(_ state: String) -> (String, Color) {
        switch state {
        case "ok": return ("checkmark.circle.fill", .green)
        case "fix": return ("exclamationmark.triangle.fill", .orange)
        case "note": return ("info.circle", .secondary)
        default: return ("questionmark.circle", .secondary)
        }
    }

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                Text(result.fix == 0 ? L("\(result.title) 配好了") : L("\(result.title) 配好了，还有 \(result.fix) 项要处理")).fontWeight(.medium)
                ForEach(result.items) { item in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Image(systemName: icon(item.state).0).foregroundStyle(icon(item.state).1)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(item.title)
                            if !item.hint.isEmpty { Text(item.hint).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }
                        }
                    }
                }
            }
            .padding(4)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// 这台 Mac 准备好了没有：逐项自动检测，写明为什么；要改系统设置的由用户本人在系统的框里确认
struct PrepareList: View {
    @EnvironmentObject var console: Console
    @State private var info: PrepareInfo?
    @State private var note = ""

    private func load() {
        DispatchQueue.global().async {
            let decoded = try? JSONDecoder().decode(PrepareInfo.self, from: Data(Backend.run(["prepare"]).out.utf8))
            DispatchQueue.main.async { info = decoded }
        }
    }

    private func line(_ ok: Bool?, _ title: String, _ why: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: ok == true ? "checkmark.circle.fill" : ok == false ? "circle" : "minus.circle")
                .foregroundStyle(ok == true ? Color.green : Color.secondary)
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                Text(why).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(L("这台的准备")).font(.headline)
                Spacer()
                Button(L("重新检查")) { load() }
            }
            line(info?.remote_login, L("远程登录"), L("主机靠它连这台，断电重启后也靠它解锁。系统设置 → 通用 → 共享 → 远程登录。"))
            line(info?.autorestart, L("断电后自动开机"), L("来电后自己开机，不用到机器前按电源键。"))
            line(info?.no_sleep, L("不自动休眠"), L("休眠了模型就停了。只管机器，屏幕照常息屏。"))
            line(info?.can_poweroff, L("允许控制台关这台"), L("只放行关机这一条命令。没设的话，卡片上的“关机”用不了，只能用苹果菜单关机。"))
            if info?.filevault == true {
                line(nil, L("FileVault 开着"), L("这是好事，不用关。断电重启后要先解锁：主机上点解锁，或到机器前输密码。"))
            }
            // A8（13.6 第 9 条）：配对时对照。主机那边终端里第一次连这台会显示同一串
            if let key = info?.host_key {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "key.horizontal").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(L("这台的主机指纹"))
                        Text(verbatim: key).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                        Text(L("主机配对这台时，终端里会让你确认一串指纹，和这一串一样才是这台。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            HStack {
                if info?.remote_login == false {
                    Button(L("打开系统设置")) {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.Sharing-Settings.extension") { NSWorkspace.shared.open(url) }
                    }
                }
                if let info = info, !(info.autorestart && info.no_sleep && info.can_poweroff) {
                    Button(L("一次设好：断电开机、不休眠、允许关机…")) {
                        note = console.applyPrepare(info.command) ? L("已设好。") : L("没有设：系统的密码框取消了，或者不是管理员账号。")
                        load()
                    }
                    .help(L("系统会弹密码框，由你本人确认。这是仅有的要动系统设置的一步"))
                }
                if !note.isEmpty { Text(note).font(.callout).foregroundStyle(.secondary) }
            }
        }
        .onAppear { load() }
    }
}

struct MachineSheet: View {
    enum Tab: Hashable { case order, add, here }

    @EnvironmentObject var console: Console
    @Binding var shown: Bool
    @State var tab: Tab = .order
    @State private var machines: MachineList?
    @State private var grants: GrantList?
    @State private var address = ""
    @State private var user = NSUserName()
    @State private var title = ""
    @State private var pairing: PairInfo?
    @State private var failure: String?
    @State private var incoming = ""
    @State private var level = "admin"
    @State private var adding = false
    @State private var pairName: String?         // 正在走密码配对、等终端里做完的那台
    @State private var pairPolling = false
    @State private var pairNote: String?         // 做到哪了，或者没成的原因
    @State private var pairFailed = false
    @State private var checking: String?
    @State private var checkResult: MachineCheck?
    private let pairTimer = Timer.publish(every: 2, on: .main, in: .common).autoconnect()

    private let levels = [("admin", Backend.showsHub ? L("能管理（包括关机、子密钥、移交主机）") : L("能管理（包括关机、子密钥、打开这台的 oMLX 管理页）")),
                          ("control", L("能操作")), ("view", L("只能看状态")), ("call", L("只调模型"))]
    private func word(_ level: String) -> String { levels.first { $0.0 == level }?.1 ?? level }

    /// 这两份不在 status 里：打开时读一次，每次操作做完再读一次
    private func load() {
        DispatchQueue.global().async {
            let m = try? JSONDecoder().decode(MachineList.self, from: Data(Backend.run(["machine", "list", "--json"]).out.utf8))
            let g = try? JSONDecoder().decode(GrantList.self, from: Data(Backend.run(["grants", "--json"]).out.utf8))
            DispatchQueue.main.async { machines = m; grants = g }
        }
    }

    /// 地址填好后猜它叫什么，默认就是那台的系统名称；猜不到就空着，连上以后用系统名称
    private func probe() {
        let ip = address.trimmingCharacters(in: .whitespaces)
        guard title.isEmpty, ip.range(of: #"^\d+\.\d+\.\d+\.\d+$"#, options: .regularExpression) != nil else { return }
        DispatchQueue.global().async {
            struct Guess: Decodable { let title: String }
            let guess = try? JSONDecoder().decode(Guess.self, from: Data(Backend.run(["machine", "probe", ip]).out.utf8))
            DispatchQueue.main.async { if title.isEmpty, address.trimmingCharacters(in: .whitespaces) == ip { title = guess?.title ?? "" } }
        }
    }

    private func add() {
        let ip = address.trimmingCharacters(in: .whitespaces)
        let name = Chip.machineName(address: ip)   // 内部用的名字，界面上不显示
        let args = ["machine", "add", name, ip, user.trimmingCharacters(in: .whitespaces)] + (title.isEmpty ? [] : [title]) + ["--json"]
        guard !adding else { return }
        adding = true
        failure = nil
        DispatchQueue.global().async {
            let r = Backend.run(args)
            let info = try? JSONDecoder().decode(PairInfo.self, from: Data(r.out.utf8))
            DispatchQueue.main.async {
                adding = false
                if let info = info, info.ok { pairing = info } else {
                    struct Failed: Decodable { let error: String? }
                    let said = try? JSONDecoder().decode(Failed.self, from: Data(r.out.utf8))
                    let text = said?.error ?? r.err.replacingOccurrences(of: "✕ ", with: "").trimmingCharacters(in: .whitespacesAndNewlines)
                    failure = text.isEmpty ? L("添加失败（退出码 \(r.code)）。") : text
                }
                load()
            }
        }
    }

    /// 自己的机器（已定第 13 条）：打开终端，在那里输一次对方的 Mac 密码。密码直接交给系统的 ssh，这里只等结果
    private func pairByPassword() {
        let ip = address.trimmingCharacters(in: .whitespaces)
        let name = Chip.machineName(address: ip)
        let args = ["machine", "pair", name, ip, user.trimmingCharacters(in: .whitespaces)] + (title.isEmpty ? [] : [title]) + ["--json"]
        guard !adding else { return }
        adding = true
        failure = nil; pairing = nil; checkResult = nil; pairNote = nil; pairFailed = false
        DispatchQueue.global().async {
            let r = Backend.run(args)
            struct Reply: Decodable { let ok: Bool? }
            let started = lastJSON(r.out, as: Reply.self)?.ok == true
            DispatchQueue.main.async {
                adding = false
                if started {
                    pairName = name
                    pairNote = L("已打开终端：在那里先确认对方的指纹，再输入对方的 macOS 登录密码。配好后这里会自己接着做。")
                } else { failure = failureText(r) }
                load()
            }
        }
    }

    /// 终端里做完没有：每 2 秒问一次。配好了就接着验证、自检；没配成就把原因写出来
    private func pollPair() {
        guard let name = pairName, !pairPolling else { return }
        pairPolling = true
        DispatchQueue.global().async {
            struct Reply: Decodable { let state: String?; let message: String? }
            let reply = lastJSON(Backend.run(["machine", "pair-status", name, "--json"]).out, as: Reply.self)
            DispatchQueue.main.async {
                pairPolling = false
                guard pairName == name else { return }
                if reply?.state == "done" {
                    pairName = nil
                    pairNote = L("配好了，正在验证…")
                    console.verifyMachine(name) { ok in
                        pairFailed = !ok
                        pairNote = ok ? nil : L("对方那边配好了，但验证没过：看下面的提示，或者点“验证”再试一次。")
                        if ok { runCheck(name) }
                    }
                } else if reply?.state == "failed" {
                    pairName = nil
                    pairFailed = true
                    pairNote = reply?.message ?? L("没配成。")
                }
            }
        }
    }

    private func runCheck(_ name: String) {
        guard checking == nil else { return }
        checking = name
        checkResult = nil
        DispatchQueue.global().async {
            let r = Backend.run(["machine", "check", name, "--json"])
            let result = lastJSON(r.out, as: MachineCheck.self)
            DispatchQueue.main.async {
                checking = nil
                checkResult = result
                if result == nil { pairFailed = true; pairNote = L("没查成：\(failureText(r))") }
            }
        }
    }

    @ViewBuilder private var pairProgress: some View {
        if let note = pairNote {
            HStack(spacing: 8) {
                if pairName != nil { ProgressView().controlSize(.small) }
                Text(note).font(.callout).foregroundStyle(pairFailed ? Color.red : Color.secondary).fixedSize(horizontal: false, vertical: true)
                if pairName != nil { Button(L("不等了")) { pairName = nil; pairNote = nil }.controlSize(.small) }
            }
        }
        if checking != nil { HStack(spacing: 8) { ProgressView().controlSize(.small); Text(L("正在检查…")).font(.callout).foregroundStyle(.secondary) } }
        if let result = checkResult { MachineCheckList(result: result) }
    }

    private func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(L("机器")).font(.title2.bold())
                Spacer()
                Picker("", selection: $tab) {
                    Text(L("顺序和名字")).tag(Tab.order)
                    Text(L("添加机器")).tag(Tab.add)
                    Text(L("本机授权")).tag(Tab.here)
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .fixedSize()
            }
            Group {
                switch tab {
                case .order: orderTab
                case .add: Backend.showsPairing ? AnyView(addTab) : AnyView(PairingTeaser(page: .add))
                case .here: Backend.showsPairing ? AnyView(hereTab) : AnyView(PairingTeaser(page: .here))
                }
            }
            Divider()
            HStack {
                if let r = console.lastResult { Text(r.text).font(.callout).foregroundStyle(r.ok ? Color.secondary : Color.red).lineLimit(2) }
                Spacer()
                Button(L("完成")) { shown = false }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(22)
        .frame(width: 680)
        .onAppear { load(); console.checkHub() }
        .onChange(of: console.running) { if console.running == nil { load() } }
        .onReceive(pairTimer) { _ in pollPair() }
    }

    private var orderTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            OrderList().environmentObject(console)
            if let rows = machines?.machines.filter({ $0.transport == "rpc" }), !rows.isEmpty {
                Divider()
                ForEach(rows) { m in
                    HStack {
                        Text(m.title)
                        Text(L("已配对")).font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        Button(L("检查")) { runCheck(m.name) }.disabled(checking != nil)
                            .help(L("查六项：看得到、能登录、oMLX 在、能关机、FileVault、地址固定。只读"))
                        Button(L("移除…")) { console.removeMachine(m.name, m.title) }.disabled(console.running != nil)
                    }
                }
                if tab == .order { pairProgress }
            }
        }
    }

    /// 新手照着做也行：先到新机器上，再回这里填，再回新机器点允许
    private var addTab: some View {
        VStack(alignment: .leading, spacing: 10) {
            step(1, L("到新机器上"), L("打开「远程登录」：系统设置 → 通用 → 共享 → 远程登录。那一页写着它的地址和账号，形如 ssh 账号@地址。装了控制台的话，它的「机器 → 本机授权」里有完整清单。"))
            step(2, L("回这里填"), L("名称默认是那台的系统名称，可以改。"))
            Grid(alignment: .leading, horizontalSpacing: 10, verticalSpacing: 8) {
                GridRow {
                    Text(L("地址")).foregroundStyle(.secondary)
                    TextField(L("192.168.1.20 或 my-mac.local"), text: $address).onSubmit { probe() }.onChange(of: address) { probe() }
                }
                GridRow {
                    Text(L("账号")).foregroundStyle(.secondary)
                    TextField(L("对方的登录账号"), text: $user)
                }
                GridRow {
                    Text(L("名称")).foregroundStyle(.secondary)
                    TextField(L("留空就用那台的系统名称"), text: $title)
                }
            }
            .textFieldStyle(.roundedBorder)
            HStack {
                if console.hub?.enabled == true {
                    Button(L("生成配对串")) { add() }
                        .buttonStyle(.borderedProminent)
                        .disabled(address.isEmpty || user.isEmpty || adding)
                } else {
                    Button(L("输密码配对（自己的机器）")) { pairByPassword() }
                        .buttonStyle(.borderedProminent)
                        .disabled(address.isEmpty || user.isEmpty || adding || pairName != nil)
                        .help(L("会打开终端，在那里输一次对方的 macOS 登录密码。密码直接交给系统，控制台不经手"))
                    Button(L("生成配对串（别人的机器）")) { add() }
                        .disabled(address.isEmpty || user.isEmpty || adding || pairName != nil)
                        .help(L("不用知道对方的密码：把配对串发给对方，由对方在自己的屏幕上点允许"))
                }
                if let failure = failure { Text(failure).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            }
            pairProgress
            ForEach(machines?.pending ?? []) { m in
                HStack {
                    Text(m.title)
                    Text(L("\(m.ip) · 等对方允许")).font(.callout).foregroundStyle(.orange)
                    Spacer()
                    Button(L("验证")) { console.verifyMachine(m.name) }.disabled(console.running != nil)
                    Button(L("移除…")) { console.removeMachine(m.name, m.title) }.disabled(console.running != nil)
                }
            }
            if let p = pairing {
                step(3, L("回到新机器上允许"), L("打开它的控制台：机器 → 本机授权，贴上配对串。两边核对码一样才点允许。配对串不是秘密，用什么传都行，不要求同一个苹果账号。"))
                GroupBox {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(L("核对码")).foregroundStyle(.secondary)
                            Text(p.code).font(.system(.title2, design: .monospaced).bold()).textSelection(.enabled)
                            Spacer()
                            Button(L("复制配对串")) { copy(p.pair) }
                            Button(L("验证")) { console.verifyMachine(p.name) }.disabled(console.running != nil)
                        }
                    }
                    .padding(4)
                }
            }
        }
    }

    private func step(_ number: Int, _ title: String, _ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("\(number)").font(.callout.weight(.semibold)).monospacedDigit().foregroundStyle(.secondary).frame(width: 14)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).fontWeight(.medium)
                Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// 这台被谁控制：准备清单、授权、允许新机器（系统对话框里亲手点）；吊销同时断开已有连接
    private var hereTab: some View {
        VStack(alignment: .leading, spacing: 12) {
            PrepareList().environmentObject(console)
            Divider()
            Text(L("这台授权给了谁")).font(.headline)
            if let g = grants, g.grants.isEmpty { Text(L("还没有授权给别的机器。")).foregroundStyle(.secondary) }
            ForEach(grants?.grants ?? []) { g in
                HStack {
                    Text(g.title)
                    Text(word(g.level)).font(.callout).foregroundStyle(.secondary)
                    Spacer()
                    Button(L("吊销…"), role: .destructive) { console.revokeGrant(g) }.disabled(console.running != nil)
                }
            }
            if let others = grants?.others, others > 0 {
                Text(L("另外还有 \(others) 行不是控制台加的，这里不管。")).font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                TextField(L("允许新机器：贴上对方的配对串"), text: $incoming).textFieldStyle(.roundedBorder)
                Picker("", selection: $level) { ForEach(levels, id: \.0) { Text($0.1).tag($0.0) } }
                    .labelsHidden().pickerStyle(.menu).fixedSize()
                Button(L("允许…")) {
                    console.allowMachine(incoming.trimmingCharacters(in: .whitespacesAndNewlines), level: level)
                    incoming = ""
                }
                .disabled(incoming.trimmingCharacters(in: .whitespaces).isEmpty || console.running != nil)
                .help(L("系统会弹对话框显示核对码，要你亲手点允许；要和对方屏幕上的一样"))
            }
        }
    }
}

/// 公开版 0.5.1 的"添加机器""本机授权"（已定第 33 条 ②）：入口不藏，点进来是预告，说清联机以后能做什么、哪一版推出
struct PairingTeaser: View {
    enum Page { case add, here }
    let page: Page

    private var rows: [String] {
        switch page {
        case .add: return [
            L("把家里的几台 Mac 连成一组，在这一个窗口里管：每台的内存、速度、在跑什么一眼看到，每台加载不同的模型。"),
            L("一键启动、关闭、重启、关机所有机器；副机断电重启后自动接上。"),
            L("配对时核对指纹和核对码，不经手对方的密码；一台可以登记几个地址，网线断了自动换 Wi-Fi。"),
        ]
        case .here: return [
            L("这一页管\"谁能控制这台\"：别的 Mac 发来配对串，你在这里核对、点允许；随时能吊销，吊销同时断开。"),
            L("允许的时候选给多大的权限：只调模型、只能看状态、能操作，或能管理。"),
            L("还会列出被控制前要准备的几项（远程登录、断电后自动开机、不休眠、允许关机），一次设好。"),
        ]
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(L("联机：1.0 正式版推出，敬请期待"), systemImage: "sparkles").font(.title3.weight(.semibold))
            Text(L("这一版是单机版：控制台管这一台 Mac。联机以后能做的：")).foregroundStyle(.secondary)
            ForEach(rows, id: \.self) { row in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "circle.fill").font(.system(size: 5)).foregroundStyle(.secondary)
                    Text(row).fixedSize(horizontal: false, vertical: true)
                }
            }
            Divider()
            Text(L("路线：0.6 让局域网里的别的设备（手机、笔记本）调用这台的模型；1.0 正式版多台 Mac 联机。"))
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, minHeight: 240, alignment: .topLeading)
    }
}

// MARK: - 设置（用户 11-04 定）：和别的软件一样，⌘, 或右上角的设置

enum SettingsTab: Hashable { case general, updates, security, guide, about, omlx }

/// 打开设置并停在指定的那一页（0.5.2，用户 11-22 在 副机 上发现）：SettingsLink 只管开窗口，开出来是上次停的那页，
/// 橙色提示的"查看"就到不了有"升级…"的更新页
struct SettingsButton: View {
    let title: String
    let tab: SettingsTab
    @EnvironmentObject var console: Console
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Button(title) { console.settingsTab = tab; NSApp.activate(ignoringOtherApps: true); openSettings() }
    }
}

struct SettingsView: View {
    @EnvironmentObject var console: Console

    var body: some View {
        TabView(selection: $console.settingsTab) {
            GeneralSettings().tabItem { Label(L("通用"), systemImage: "gearshape") }.tag(SettingsTab.general)
            if !Backend.isManager { UpdatesSettings().tabItem { Label(L("更新"), systemImage: "arrow.down.circle") }.tag(SettingsTab.updates) }
            if Backend.showsHub { SecuritySettings().tabItem { Label(L("安全"), systemImage: "lock") }.tag(SettingsTab.security) }
            GuideSettings().tabItem { Label(L("教程"), systemImage: "book") }.tag(SettingsTab.guide)
            AboutSettings().tabItem { Label(L("关于"), systemImage: "info.circle") }.tag(SettingsTab.about)
            OMLXSettings().tabItem { Label { Text(verbatim: "oMLX") } icon: { Image(systemName: "cpu") } }.tag(SettingsTab.omlx)
        }
        .frame(width: 560, height: 540)
    }
}

struct GeneralSettings: View {
    @EnvironmentObject var console: Console
    @State private var atLogin = SMAppService.mainApp.status == .enabled
    @State private var loginFailure: String?

    var body: some View {
        Form {
            Section(L("语言")) {
                Picker(L("显示语言"), selection: Binding(get: { console.language }, set: { console.setLanguage($0) })) {
                    Text(L("跟随系统")).tag("")
                    Text(verbatim: L("简体中文")).tag("zh-Hans")
                    Text(verbatim: "English").tag("en")
                }
            }
            if !Backend.isManager {
                Section(L("启动")) {
                    Toggle(L("打开控制台时启动 oMLX"), isOn: $console.autoStart)
                    Text(L("主机还会把副机的 oMLX 和控制台一起打开。不会自动加载模型。")).font(.callout).foregroundStyle(.secondary)
                    Toggle(L("登录 Mac 时打开控制台"), isOn: Binding(get: { atLogin }, set: { on in
                        do {
                            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                            atLogin = on
                            loginFailure = nil
                        } catch { loginFailure = L("没有改成：\(error.localizedDescription)") }
                    }))
                    if let failure = loginFailure { Text(failure).font(.callout).foregroundStyle(.red) }
                }
            }
            Section(L("关闭窗口")) {
                Toggle(L("关闭窗口时退出控制台"), isOn: $console.quitOnClose)
                Text(L("关着时，点红色按钮只收起窗口，控制台和 oMLX 照常运行。打开后，点红色按钮就是退出控制台，和 ⌘Q 一样先确认。")).font(.callout).foregroundStyle(.secondary)
            }
            Section(L("提醒")) {
                Toggle(L("内存超线、模型卡住时让 Dock 图标跳动"), isOn: $console.bounceDock)
            }
        }
        .formStyle(.grouped)
    }
}

/// B3（用户 11-21 定）：设置的最后一页"oMLX"。最上面两个显眼的按钮，下面才是这台 oMLX 的种类、版本、主密钥、入口（原来在通用页）。
/// 两个按钮打开的是同一个网页管理页的不同页面；oMLX 应用自己的设置窗口控制台不去开
struct OMLXSettings: View {
    @EnvironmentObject var console: Console
    @State private var which = ""

    private var target: String? { nodeOrder.contains(which) ? which : (console.localKey ?? nodeOrder.first) }
    private var canOpen: Bool {
        guard !Backend.isManager, let key = target, let n = console.node(key) else { return false }
        return n.up && n.reachable && !console.stale
    }

    private var hint: String {
        if Backend.isManager { return L("管理页要在主机上打开。") }
        guard let key = target, let n = console.node(key) else { return L("读取中…") }
        if !n.reachable { return L("连不上 \(console.machine(key))。") }
        if !n.up { return L("oMLX 没在运行：启动以后才能打开。") }
        return L("「oMLX 设置」是服务器、模型、缓存、网络这些全局设置；「oMLX 管理页」是仪表盘、下载模型、聊天、日志。控制台代为登录，不用输密钥。")
    }

    var body: some View {
        Form {
            Section {
                if nodeOrder.count > 1 {
                    Picker(L("哪台"), selection: Binding(get: { target ?? "" }, set: { which = $0 })) {
                        ForEach(nodeOrder, id: \.self) { Text(console.machine($0)).tag($0) }
                    }
                }
                HStack(spacing: 12) {
                    Button { if let key = target { console.openAdmin(key, settings: true) } } label: {
                        Label(L("打开 oMLX 设置"), systemImage: "slider.horizontal.3").frame(minWidth: 180)
                    }
                    .buttonStyle(.borderedProminent)
                    Button { if let key = target { console.openAdmin(key) } } label: {
                        Label(L("打开 oMLX 管理页"), systemImage: "macwindow").frame(minWidth: 180)
                    }
                    .buttonStyle(.bordered)
                }
                .controlSize(.large)
                .disabled(!canOpen)
                Text(hint).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if console.engine?.official == true {
                Section(L("模型文件夹")) {
                    Button(L("添加已有的模型文件夹…")) { console.addModelsFolder() }
                        .disabled(console.busy || !canOpen || console.engine?.keyBlocked == true)
                    Text(L("硬盘上已经有 MLX 格式的模型时用：选它们所在的文件夹，oMLX 会认出来，不用重新下载。只加一行，oMLX 自己下载模型的地方不变；控制台和 oMLX 都不往这个文件夹里写。"))
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            if let e = console.engine { EngineSection(engine: e) }
        }
        .formStyle(.grouped)
    }
}

/// 这台的 oMLX（第 3 期）：种类、版本落在兼容表哪一档、主密钥允不允许。自管版（你两台）没有这一节
struct EngineSection: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow
    let engine: EngineInfo

    private var gatewayText: String {
        switch engine.gateway {
        case "running": return L("在运行，客户端连端口 \(engine.client_port.map { String($0) } ?? "8900")")
        case "elsewhere": return L("开了主机模式：入口在主机上")
        case "off": return L("关着：客户端直连 oMLX，没加载的模型会被点起来")
        default: return engine.gateway_note.map { L("没在运行：\($0)") } ?? L("没在运行：启动 oMLX 或重开控制台时会再起")
        }
    }

    private var keyText: String {
        switch engine.key {
        case "allowed": return L("已允许控制台读")
        case "denied": return L("没允许，管理功能用不了")
        default: return L("还没问过")
        }
    }

    var body: some View {
        Section {
            LabeledContent(L("种类"), value: (engine.kind_text ?? engine.kind) + (engine.how_text.map { L("（\($0)）") } ?? ""))
            if engine.kind == "none" { Button(L("安装 oMLX…")) { console.openSetup(.install, open: openWindow) } }
            if engine.official {
                LabeledContent(L("版本")) {
                    Text((engine.version ?? L("读不到")) + (engine.verdict_text.map { L("，\($0)") } ?? ""))
                        .foregroundStyle(engine.warnsVersion ? Color.orange : Color.secondary)
                }
                if let note = engine.note { Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                LabeledContent(L("主密钥")) {
                    HStack(spacing: 8) {
                        Text(keyText).foregroundStyle(engine.keyBlocked ? Color.orange : Color.secondary)
                        if engine.key == "allowed" {
                            Button(L("改成不允许")) { console.setEngineKey(false) }
                        } else {
                            Button(L("允许…")) { console.askEngineKeyNow() }
                        }
                    }
                }
                Text(L("控制台要读 oMLX 的主密钥，才能加载和卸载模型、管理子密钥、看日志和多轮缓存。只记允许还是不允许，不存密钥，每次用时现读。"))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Toggle(isOn: Binding(get: { engine.gateway != "off" }, set: { console.setGateway($0) })) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(L("入口"))
                        HStack(spacing: 8) {
                            Text(gatewayText).font(.callout).foregroundStyle(engine.gateway == "running" || engine.gateway == "elsewhere" ? Color.secondary : Color.orange)
                            if engine.gateway == "stopped" { Button(L("现在启动")) { console.runGateway("ensure") }.controlSize(.small) }
                        }
                    }
                }
                .disabled(engine.gateway == "elsewhere")
                Text(L("客户端连入口，不直接连 oMLX：入口认子密钥，只放行已加载的模型，挡住加载、卸载和管理页。官方 oMLX 的子密钥能直接加载模型，所以默认开着。每次请求多约 1 毫秒。"))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if let e = console.engineError { Text(e).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true).textSelection(.enabled) }
            }
        } header: { Text(verbatim: "oMLX") }
    }
}

/// 最高密码只在这里设。走安全输入框和标准输入，不进命令行、日志、剪贴板
struct SecuritySettings: View {
    @EnvironmentObject var console: Console

    private var hub: HubView? { console.hub }
    private var isSet: Bool { hub?.password_set == true }
    private var canEdit: Bool { !Backend.isManager && (hub?.enabled != true || hub?.is_hub == true) }

    private func point(_ title: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).fontWeight(.medium)
            Text(text).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var chip: ChipView? { console.chip }
    private var registered: Bool { chip?.keys.contains { $0.id == console.chipKeyId } == true }
    private var requested: Bool { chip?.requests.contains { $0.id == console.chipKeyId } == true }

    private var hereText: String {
        if !Chip.isAvailable { return L("没有安全芯片，用不了这一项") }
        if console.chipLost { return L("钥匙失效了（抹过盘或换过主板），要重新登记") }
        guard let chip = chip else { return L("还没读到") }
        // 这台的指纹当场按钥匙算，不用后端说的：别的机器接受登记时拿它当标准
        if chip.keys.contains(where: { $0.id == console.chipKeyId }) { return L("已登记，指纹 \(Backend.isPreview ? chip.keys[0].fingerprint : Chip.publicKey().map { Chip.fingerprint(publicText: $0) } ?? L("读不出来"))") }
        return requested ? L("已申请，等批准") : L("还没登记")
    }

    private func keyRow(_ key: ChipView.Key) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(key.title)
                    if key.id == console.chipKeyId { Tag(text: L("本机")) }
                }
                Text(L("\(key.fingerprint)，\(key.added) 登记")).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if registered && !Backend.isManager {
                Button(L("吊销…")) { console.chipRevoke(key) }
                    .disabled((chip?.keys.count ?? 0) < 2)
                    .help((chip?.keys.count ?? 0) < 2 ? L("这是最后一把，吊销了就没有机器能做最高操作了") : L("机器丢了、卖了就吊销"))
            }
        }
    }

    private func requestRow(_ request: ChipView.Request) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(L("\(request.title) 申请登记")).fontWeight(.medium)
                Text(L("指纹 \(Chip.fingerprint(publicText: request.pub))，来自 \(request.where ?? L("别的机器"))。10 分钟不批就失效。")).font(.callout).foregroundStyle(.secondary)
            }
            Spacer()
            if registered && !Backend.isManager { Button(L("批准…")) { console.chipApprove(request) } }
            else if Backend.isManager { Text(L("到主机上批准")).font(.callout).foregroundStyle(.secondary) }
        }
    }

    @State private var showAdvanced = false

    /// 开了任何一样，或者有要处理的登记，就一直展开，免得提示被收起来看不到
    private var advancedInUse: Bool {
        isSet || chip?.enabled == true || chip?.pending != nil || hub?.enabled == true
            || !console.hubConflicts.isEmpty || !console.hubUnjoined.isEmpty
    }

    @ViewBuilder private var passwordSection: some View {
        Section(L("最高密码")) {
            LabeledContent(L("状态"), value: isSet ? L("已设置") + (hub?.password_at.map { L("，\($0) 改过") } ?? "") : L("还没设"))
            if hub?.locked == true { Text(L("连错太多次，暂时锁定了。")).foregroundStyle(.red) }
            Text(L("设了以后，关机要多输一次。启用主机模式必须先设。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if canEdit {
                HStack {
                    if isSet {
                        Button(L("修改…")) { console.hubChangePassword() }.disabled(console.running != nil)
                        Button(L("忘记密码…")) { console.forgotPassword() }.disabled(console.running != nil)
                    } else {
                        Button(L("设密码…")) { console.setPassword() }.disabled(console.running != nil)
                    }
                    Spacer()
                }
            } else {
                Text(L("只能在主机本机改。")).font(.callout).foregroundStyle(.secondary)
            }
        }
    }

    @ViewBuilder private var chipSection: some View {
        Section {
            LabeledContent(L("这台 Mac"), value: hereText)
            if let chip = chip {
                if let root = chip.root { LabeledContent(L("登记表"), value: L("指纹 \(root)，共 \(chip.seq) 条")) }
                if let waiting = chip.waiting, !waiting.isEmpty {
                    Text(L("\(waiting.joined(separator: L("、"))) 还没有接受：到那台的 设置 → 安全 核对后接受，在那之前它不查签名。")).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                if !console.hubUnjoined.isEmpty {
                    Text(L("\(console.hubUnjoined.joined(separator: L("、"))) 还没跟上这台的状态：可能还没接受登记，也可能签这份状态的钥匙已经换了。先让它接受，再重新签一次。")).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                if hub?.needs_resign == true || !console.hubUnjoined.isEmpty {
                    HStack {
                        if hub?.needs_resign == true { Text(L("这台的主机状态是旧钥匙签的，别的机器过不了。")).font(.callout).foregroundStyle(.orange) }
                        Spacer()
                        if registered && !Backend.isManager { Button(L("重新签一次…")) { console.hubResign() } }
                    }
                }
                if !console.hubConflicts.isEmpty {
                    Text(L("\(console.hubConflicts.joined(separator: L("、"))) 和这台对主机的说法不一样：可能主机离线时两边各改过，也可能那台被动过。入口先不开。查清楚后点重新签一次，各台就会一致。")).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                ForEach(chip.keys) { keyRow($0) }
                if let pending = chip.pending {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(L("收到一份登记，还没生效")).fontWeight(.medium)
                            Text(L("第一把钥匙：\(pending.title)，登记表指纹 \(pending.root)，共 \(pending.seq) 条。到登记第一把钥匙的那台（通常是主机）的这一页核对，一样才接受。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                        }
                        Spacer()
                        if Backend.isManager { Text(L("到主机上接受")).font(.callout).foregroundStyle(.secondary) }
                        else { Button(L("接受…")) { console.chipAccept() } }
                    }
                }
                ForEach(chip.requests) { requestRow($0) }
                if chip.enabled { HStack { Spacer(); Button(L("刷新")) { console.checkChip() } } }
                if Chip.isAvailable && !registered {
                    HStack {
                        if !chip.enabled && chip.is_hub == true && !Backend.isManager {
                            Button(L("登记这台…")) { console.chipRegisterFirst() }
                        } else if !chip.enabled && Backend.isManager {
                            Text(L("先到主机上登记第一台。")).font(.callout).foregroundStyle(.secondary)
                        } else {
                            Button(requested ? L("再申请一次") : L("申请登记…")) { console.chipRequest() }
                            if chip.enabled { Button(L("用恢复码登记…")) { console.chipRecover() } }
                            if !chip.enabled && !Backend.isManager { Button(L("开始新的登记…")) { console.chipStartNew() }.help(L("只有主机没了才用")) }
                        }
                        Spacer()
                    }
                }
            }
        } header: { Text(L("安全芯片")) } footer: {
            Text(L("登记后，关机、移交这些操作要这台 Mac 的安全芯片签名，别的机器也会核对。你没点操作时弹出来的框不要确认。"))
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        if chip?.enabled == true {
            Section(L("恢复码")) {
                LabeledContent(L("状态"), value: chip?.recovery_at.map { L("\($0) 生成") } ?? L("已生成"))
                Text(L("登记的机器都没了时，靠它登记一把新钥匙，用一次就换新的。这里不保存恢复码。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if registered && !Backend.isManager {
                    HStack { Button(L("换一个新的…")) { console.chipNewRecovery() }; Spacer() }
                }
            }
        }
    }

    @ViewBuilder private var rescueSection: some View {
        Section(L("万一出事")) {
            if isSet { point(L("忘了最高密码"), L("在主机点忘记密码，过 macOS 管理员密码框，设一个新的。日常使用不受影响。")) }
            if chip?.enabled == true {
                point(L("指纹不认、手指磨损、键盘坏了"), L("系统会让你改输 Mac 密码。增删指纹不影响钥匙。"))
                point(L("一台的钥匙没了（抹盘、换主板、机器丢了）"), L("在另一台登记过的机器上吊销旧的，这台重新申请。"))
                point(L("登记的机器全没了"), L("在这台点用恢复码登记。"))
                point(L("恢复码也丢了"), L("到每台机器前，过 macOS 管理员验证，清掉登记，重新登记、接受，最后在主机点重新签一次。人到场总能恢复，模型和数据不受影响。"))
            }
            if !Backend.isManager {
                HStack {
                    if chip?.enabled == true || chip?.pending != nil { Button(L("清掉这台的登记…")) { console.chipForget() } }
                    if hub?.enabled == true { Button(L("退出主机模式…")) { console.hubLeave() }.help(L("主机没了、最高密码也丢了才用；主机钥匙的授权也一起撤掉")) }
                    Spacer()
                }
            }
        }
    }

    var body: some View {
        Form {
            Section(L("关机怎么确认")) {
                Text(advancedInUse ? L("点确认，再按一下指纹或输 Mac 密码。开了下面的高级保护，就按开的来。") : L("点确认，再按一下指纹或输 Mac 密码。不用提前设置，直接能用。")).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if !advancedInUse {
                Section {
                    Button { showAdvanced.toggle() } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(L("高级保护（可选）")).fontWeight(.medium)
                                Text(L("自己在局域网里用，不开也可以。多人共用、以后要远程控制时再开。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            }
                            Spacer()
                            Image(systemName: showAdvanced ? "chevron.up" : "chevron.down").foregroundStyle(.secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            if advancedInUse || showAdvanced {
                passwordSection
                chipSection
            }
            if advancedInUse && (isSet || chip?.enabled == true || chip?.pending != nil || hub?.enabled == true) { rescueSection }
        }
        .formStyle(.grouped)
        .onAppear { console.checkHub(); console.checkChip() }
    }
}

/// 使用教程：给第一次用的人。文字都在 guideSections，以后翻译只改这一处；**加粗**的是界面上的按钮名，改按钮名要同步
struct GuideSection: Identifiable {
    let title: String
    let rows: [String]
    var key: String = ""   // 程序用它认小节（管理版不显示哪几节），不认标题文字
    var id: String { key.isEmpty ? title : key }
}

let guideForManager = GuideSection(title: L("这一份是管理版"), rows: [
    L("管理版是遥控器，不跑模型。点左下角**连接**连上主机，之后的操作都由主机执行。"),
    L("它能关每一台机器，也能全部关机，方便人不在时远程切断。关机后远程开不回来，只能到机器上按电源键。"),
    L("退出管理版什么都不会停，模型和服务照常跑。"),
    L("主机开了安全芯片的话，先到**安全**页点**申请登记**，回主机批准。"),
])

/// 教程正文。dsh：这台检测到了 dsh 才讲它那一句；讲主机模式、最高密码、安全芯片的几句只在 Backend.showsHub 时出现（公开版这次不发布）
func guideSections(dsh: Bool) -> [GuideSection] {
    let hub = Backend.showsHub, pairing = Backend.showsPairing   // pairing：公开版 0.5.1 是单机版，联机那一章换成预告（已定第 33 条）
    return [
    GuideSection(title: L("这是什么"), rows: [
        L("把开源大模型跑在你自己的 Mac 上。启动服务、加载和切换模型、发密钥给客户端，都在这一个窗口里。"),
        L("模型由 oMLX 运行，这个窗口是它的开关和仪表盘。"),
    ]),
    GuideSection(title: L("第一次跑通一个模型"), rows: [
        L("1. 新装的 Mac：窗口上方有**开始使用**三步，照着做就行。三步做完它会自己收起；想再看，这一页最上面有按钮。"),
        L("2. 第一步点**安装 oMLX…**，控制台帮你装官方版。已经装了、只是没启动的，点**启动 oMLX**。机器卡片上的点变**绿**，就是服务在跑。"),
        L("3. 第二步点**打开 oMLX 的管理页**，在里面下载一个模型，下完回到这个窗口，模型卡片会自己出现。硬盘上已经有 MLX 格式的模型，就点**添加已有的模型文件夹…**，不用重新下载。要是先看到**允许读主密钥…**，点它：控制台有了这把钥匙才看得到这台有哪些模型，钥匙只在这台上用。"),
        L("4. 第三步挑一个模型，点**加载并自检**（列表里标了每个模型多大、放不放得下）。大模型要等几分钟，卡片上的按钮变成**卸载**就是加载好了，控制台接着让模型算一道题，确认它答得出来。以后想再测，点卡片上的**自检**。"),
        L("已经在用的 Mac 没有这三步：打开控制台，oMLX 会自动启动（**通用**页可以关），直接在模型卡片上点**加载**。"),
        L("5. 让客户端（你用来聊天、写代码的软件）连上：点左下角**密钥**，里面写着要填的三样——地址（`http://127.0.0.1:8800/v1`）、模型名、密钥，都能一键复制；密钥在那一页**新建并复制**一把子密钥，一个客户端一把。"),
    ] + (dsh ? [L("用 dsh 的话，选模型卡片最下面写的那一项。")] : []), key: "first-run"),
    GuideSection(title: L("窗口上的按钮"), rows: [
        L("**顶部一排**管所有机器：启动全部、关闭全部、卸载全部、重启全部、全部关机、刷新全部。现在不能按的会灰着。"),
        L("**机器卡片**：启动或关闭、重启、关机、**oMLX 管理页**。**oMLX 管理页**打开这台 oMLX 自己的网页（仪表盘、下载模型、聊天、日志），不用输密钥。这台自己的卡片也能关机，关了这个窗口跟着关掉。多轮缓存也在这里选。"),
        L("**点的颜色**：绿，服务在跑；红，开着但服务没跑或卡住；灰，已关机或连不上；橙，正在变。"),
        L("**模型卡片**：上下文在这里选，选项写明了推荐。放不下一起跑时按钮写**切换**，会先卸载别的模型。"),
        L("**右上角**：**设置**就是这里，最后一页 **oMLX** 能直接打开 oMLX 的设置；**排列**换排法，**长条**把窗口收成一条，**固定**把窗口钉在原处、总在最前。"),
    ]),
    GuideSection(title: L("关窗口和退出"), rows: [
        L("红色按钮只收起窗口，服务和模型继续跑，点 Dock 图标就回来。想点红色按钮就退出，到**通用**页打开「关闭窗口时退出控制台」。"),
        L("⌘Q 才是退出：oMLX 一起关，模型也卸载。退出前会告诉你还有多少请求在处理。"),
        L("主机退出等于关闭所有机器的服务；副机退出只关它自己。"),
    ], key: "close-quit"),
    pairing ? GuideSection(title: L("加一台机器"), rows: [
        L("1. 到新机器上打开「远程登录」：系统设置 → 通用 → 共享 → 远程登录。那一页写着它的地址和账号。"),
        L("2. 回这台，点左下角**机器**，到**添加机器**填地址和账号。名称可以留空，默认用那台的系统名称。"),
        L("3. 自己的机器：点**输密码配对（自己的机器）**。会打开终端，先确认一次那台的指纹，再输那台的 macOS 登录密码；密码直接交给系统，控制台不经手。配好后这边自己验证，新机器的卡片就出来了。"),
        L("4. 接着会自动查六项：看得到、能登录、oMLX 在、能关机、FileVault、地址固定。没过的那一项下面写着怎么办。以后想再查，到**机器**的**顺序和名字**页点**检查**。"),
        L("5. 别人的机器（不知道对方的密码）：点**生成配对串（别人的机器）**，把配对串发给对方。对方在自己的控制台 **机器** → **本机授权** 贴上，点**允许…**，两边核对码一样才确认；然后回这台点**验证**。"),
    ] + (hub ? [L("启用了主机模式的话，只有**生成配对串**这一条路，验证时还要指纹和最高密码。")] : []) + [
        L("副机的窗口只显示它自己，全局操作都在主机上。卡片的顺序和名字也在**机器**里调。"),
    ], key: "add-machine") : GuideSection(title: L("联机（1.0 推出）"), rows: [
        L("这一版是单机版，管这一台 Mac。多台 Mac 联机在 1.0 正式版推出：一个窗口管几台，每台跑不同的模型。"),
        L("左下角**机器**里的**添加机器**、**本机授权**两页先放着预告；**顺序和名字**现在就能改这台卡片的显示名。"),
        L("0.6 先让局域网里的别的设备（手机、笔记本）调用这台的模型。"),
    ], key: "add-machine"),
    GuideSection(title: L("关机和安全"), rows: [
        L("关机时点确认，再按一下指纹或输 Mac 密码，不用提前设置。"),
    ] + (hub ? [L("自己在局域网里用，这样就够了。想更严，到**安全**页打开高级保护：最高密码和安全芯片都是可选的，开了哪样查哪样。启用主机模式要先设最高密码。")] : []) + [
        L("你没点操作时弹出来的框不要确认。"),
        L("关机后只能到机器上按电源键才能再开。**全部关机**先关副机，确认关掉再关主机；有副机关不掉，主机就留着。"),
    ] + (pairing ? [L("一台可以登记几个地址（**机器** → **地址**），网线在前：连不上第一个就自动用下一个。")] : [])),
    GuideSection(title: L("出问题时"), rows: [
        L("先点底部的**体检**，它逐项查，告诉你哪里不对。"),
        L("**日志**是 oMLX 自己的记录，**操作记录**是这个窗口做过什么。"),
        L("客户端报错：看模型是不是已经加载，选的是不是那台机器上现在在线的那个。"),
    ] + (pairing ? [L("加机器没配成：**添加机器**页会写明原因，照着改好再点一次。")] : []) + [
        L("关机超过 3 分钟还没关掉，卡片会说卡住，到机器前长按电源键。"),
        L("oMLX 管理页里「显存上限」那行红字可以不管：控制台的安全线本来就比系统默认的显存上限低（512 GB 的 Mac 上是 461 GB 对约 464 GB）。"),
    ] + (hub ? [L("忘了最高密码、钥匙没了：看**安全**页的\"万一出事\"。")] : []) + [
        L("还解决不了：**关于**页有发布者的 GitHub。"),
    ]),
    ]
}

struct GuideSettings: View {
    @EnvironmentObject var console: Console
    /// 管理版不跑模型，这几节讲的是设备版的事
    private static let deviceOnly = ["first-run", "close-quit", "add-machine"]
    private var sections: [GuideSection] {
        let all = guideSections(dsh: console.status?.tools?.dsh == true)   // 检测到 dsh 才讲它（没读到状态时不讲）
        return Backend.isManager ? [guideForManager] + all.filter { !Self.deviceOnly.contains($0.key) } : all
    }

    var body: some View {
        Form {
            if let first = console.firstRun, first.done, !console.showSetupAgain {
                Section {
                    Button(L("再看一遍“开始使用”")) { console.showSetupAgain = true }
                }
            }
            ForEach(sections) { section in
                Section(section.title) {
                    ForEach(section.rows, id: \.self) { row in
                        // .init 让 **加粗** 和 `代码` 生效
                        Text(.init(console.guideRow(row))).font(.callout).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    }
                }
            }
        }
        .formStyle(.grouped)
    }
}

struct AboutSettings: View {
    /// 发布者、主页由 brand.py 经构建脚本写进 Info.plist（已定第 23 条）；公开仓库建好后主页换成仓库地址
    static let publisher = (Bundle.main.object(forInfoDictionaryKey: "LMCPublisher") as? String) ?? "dan-cube"
    static let homepage = (Bundle.main.object(forInfoDictionaryKey: "LMCHomepage") as? String) ?? "https://github.com/dan-cube"
    /// 许可证名：公开版的构建脚本看到 LICENSE 才写进 Info.plist（0.5.1 起 Apache-2.0）；内部的构建没有这一行
    static let license = Bundle.main.object(forInfoDictionaryKey: "LMCLicense") as? String

    var body: some View {
        Form {
            Section {
                LabeledContent(L("版本"), value: Backend.version)
                if Backend.showsHub {   // 公开版只有这一种，不提"设备版""管理版"
                    LabeledContent(L("这一份"), value: Backend.isManager ? L("管理版：遥控器，不跑模型") : L("设备版：和这台的 oMLX 同进退"))
                }
            }
            Section(L("发布者")) {
                LabeledContent(L("发布者"), value: Self.publisher)
                LabeledContent("GitHub") {
                    if let url = URL(string: Self.homepage) { Link(Self.homepage.replacingOccurrences(of: "https://", with: ""), destination: url) }
                }
                if let license = Self.license {
                    LabeledContent(L("许可证")) {
                        if let url = URL(string: Self.homepage + "/blob/main/LICENSE") { Link(license, destination: url) } else { Text(verbatim: license) }
                    }
                }
                Text(L("问题和建议可以到这里反馈。模型由 oMLX 运行，它是单独的开源项目。")).font(.callout).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: - 管理页窗口：内嵌网页，会话只在内存里，按机器隔开（两台都是 127.0.0.1，cookie 不分端口）

final class AdminWindows: NSObject, NSWindowDelegate, WKNavigationDelegate {
    static let shared = AdminWindows()
    private var windows: [String: NSWindow] = [:]
    private var bases: [String: String] = [:]

    /// 那台的管理页窗口已经开着：拿到前面；要的是设置页就翻到设置页（会话还在窗口里）
    func raise(_ key: String, page: String? = nil) -> Bool {
        guard let window = windows[key] else { return false }
        if let page = page, let base = bases[key], let url = URL(string: base + page), !url.isFileURL {
            (window.contentView as? WKWebView)?.load(URLRequest(url: url))
        }
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        return true
    }

    func open(_ key: String, title: String, session: AdminSession, page: String? = nil) {
        guard let base = URL(string: session.url) else { return }
        let url = page.flatMap { base.isFileURL ? nil : URL(string: session.url + $0) } ?? base
        bases[key] = session.url
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1120, height: 780), configuration: config)
        web.navigationDelegate = self
        let window = NSWindow(contentRect: web.frame, styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
        window.title = title
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        windows[key] = window
        NSApp.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
        if url.isFileURL {
            web.loadFileURL(url, allowingReadAccessTo: url)
            return
        }
        let cookie = HTTPCookie(properties: [.domain: url.host ?? "127.0.0.1", .path: "/", .name: session.name, .value: session.value, .originURL: url])
        if let cookie = cookie {
            config.websiteDataStore.httpCookieStore.setCookie(cookie) { web.load(URLRequest(url: url)) }
        } else {
            web.load(URLRequest(url: url))
        }
    }

    // 关窗口就放掉网页进程和会话
    func windowWillClose(_ notification: Notification) {
        guard let window = notification.object as? NSWindow else { return }
        (window.contentView as? WKWebView)?.stopLoading()
        DispatchQueue.main.async { window.contentView = nil }
        for (key, value) in windows where value === window { bases[key] = nil }
        windows = windows.filter { $0.value !== window }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        webView.loadHTMLString(L("<body style='font:14px -apple-system;padding:40px;color:#666'>打不开管理页：\(error.localizedDescription)</body>"), baseURL: nil)
    }
}

// MARK: - 浮动长条：固定一种样子，只显示核心信息（每台：现在跑的模型、内存、速度）

struct BarView: View {
    @EnvironmentObject var console: Console

    var body: some View {
        HStack(spacing: 12) {
            ForEach(nodeOrder, id: \.self) { key in
                machine(key)
                Divider().frame(height: 16)
            }
            Text(console.overLine ? L("内存超线") : L("内存正常"))
                .fontWeight(console.overLine ? .bold : .regular)
                .foregroundStyle(console.overLine ? Color.red : Color.green)
                .frame(width: 60)
            Button { console.refreshNow() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless)
                .help(L("立刻刷新"))
            Button { console.expandFromBar() } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                .buttonStyle(.borderless)
                .help(L("展开控制台"))
        }
        .font(.system(size: 12))
        .padding(.horizontal, 14)
        .frame(height: 36)
        .background(Capsule().fill(Color(nsColor: .windowBackgroundColor)))
        .overlay(Capsule().stroke(Color.secondary.opacity(0.35), lineWidth: 0.5))
        .contentShape(Capsule())
        .gesture(DragGesture(minimumDistance: 2).onChanged { _ in BarPanel.shared.drag() }.onEnded { _ in BarPanel.shared.endDrag() })
    }

    private func machine(_ key: String) -> some View {
        let n = console.node(key)
        let color = console.dot(key).color
        let speed = n?.loaded.flatMap { n?.stats[$0]?.speed_tps }.map { $0 > 0 ? " \(Int($0.rounded()))t/s" : "" } ?? ""
        return HStack(spacing: 5) {
            Text(String(console.machine(key).prefix(1))).fontWeight(.bold).foregroundStyle(machineTint(key))
            Dot(color: color, size: 8)
            Text(console.shortState(key) + speed).fontWeight(.semibold).lineLimit(1).monospacedDigit()
            MemoryBar(used: Double(n?.used_gb ?? 0), total: Double(n?.total_gb ?? max(n?.used_gb ?? 0, 1)), line: nil, tint: machineTint(key),
                      alert: n?.alert?.isEmpty == false).frame(width: 36, height: 6)
        }
        .frame(width: 168, alignment: .leading)
    }
}

/// 无边框浮动面板：浮在其他窗口上面，可以拖到任意位置，下次记住位置
final class BarPanel {
    static let shared = BarPanel()
    private var panel: NSPanel?
    private var dragStart: (mouse: NSPoint, origin: NSPoint)?

    func show(_ console: Console) {
        if panel == nil {
            let host = NSHostingView(rootView: BarView().environmentObject(console))
            let size = host.fittingSize
            let p = NSPanel(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel],
                            backing: .buffered, defer: false)
            p.contentView = host
            p.isFloatingPanel = true
            p.level = .floating
            p.backgroundColor = .clear
            p.isOpaque = false
            p.hasShadow = true
            p.hidesOnDeactivate = false
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            if !p.setFrameUsingName("LMCBar2"), let screen = NSScreen.main?.visibleFrame {
                p.setFrameOrigin(NSPoint(x: screen.maxX - size.width - 24, y: screen.maxY - size.height - 16))
            }
            p.setFrameAutosaveName("LMCBar2")
            panel = p
        }
        panel?.orderFrontRegardless()
    }

    func hide() { panel?.orderOut(nil) }

    // 用屏幕坐标算位移：面板跟着鼠标走，不会因为自己在动而抖
    func drag() {
        guard let p = panel else { return }
        let mouse = NSEvent.mouseLocation
        if dragStart == nil { dragStart = (mouse, p.frame.origin) }
        guard let start = dragStart else { return }
        p.setFrameOrigin(NSPoint(x: start.origin.x + mouse.x - start.mouse.x, y: start.origin.y + mouse.y - start.mouse.y))
    }

    func endDrag() {
        dragStart = nil
        panel?.saveFrame(usingName: "LMCBar2")
    }
}

// MARK: - 菜单栏

struct MenuBarContent: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if console.overLine {
            ForEach(console.memoryAlert, id: \.self) { Text(L("⚠️ 内存超线：") + $0) }
            if console.hasOllama {
                Button(L("让 Ollama 释放…")) { console.freeOllama() }
                    .disabled(console.running != nil || (console.status?.ollama ?? []).isEmpty)
            }
            Divider()
        }
        if console.stuck {
            Text("⏸ " + (console.progress?.text ?? L("可能卡住了")))
            Divider()
        }
        Text(console.status?.mode_text ?? L("正在读取状态…"))
        Divider()
        Button(L("打开控制台窗口")) {
            console.openWindowAction = openWindow
            console.expandFromBar()
        }
        Button(L("收起为长条")) { console.collapseToBar() }
        ForEach(nodeOrder, id: \.self) { key in
            Divider()
            let n = console.node(key)
            Text(L("\(console.machine(key))：\(console.sentence(key))"))
            if n?.up == true {
                ForEach(console.models(on: key), id: \.self) { m in
                    Button(L("在 \(console.machine(key)) 上加载 \(console.title(m, on: key))")) { console.load(m, on: key) }
                        .disabled(console.busy || (n?.models[m] ?? "off") != "off")
                }
                ForEach(n?.active ?? [], id: \.self) { m in
                    Button(L("卸载 \(console.machine(key)) 的 \(console.title(m, on: key))…")) { console.unload(m, on: key) }.disabled(console.busy || n?.models[m] != "loaded")
                }
            } else if n?.reachable != false {
                Button(L("启动 \(console.machine(key)) 的 oMLX 服务")) { console.startService(key) }.disabled(console.busy)
            }
        }
        Divider()
        if nodeOrder.count > 1 {
            Button(L("启动全部")) { console.startService() }.disabled(console.busy || !console.anyDown)
            Button(L("关闭全部…")) { console.closeAll() }.disabled(console.busy || !console.anyUp)
            Divider()
        }
        SettingsLink { Text(L("设置…")) }
        Button(L("退出控制台…")) { NSApp.terminate(nil) }
    }
}

// MARK: - 第 3b 期：安装和升级 oMLX、更新中心、通知（草案 10.5–10.8；和后端的约定见工作副本 p3b/CONTRACT.md）
// 数据都走 engine、updates 的新命令，不进 status：自管版（你两台）主界面一个字不差，只在设置里多一页"更新"

/// 数组里哪一条读不懂就跳过那一条，不把整个回复读坏
struct LenientList<T: Decodable>: Decodable {
    var items: [T] = []
    private struct Skip: Decodable {}

    init(from decoder: Decoder) throws {
        guard var c = try? decoder.unkeyedContainer() else { return }
        while !c.isAtEnd {
            if let item = try? c.decode(T.self) { items.append(item) }
            else if (try? c.decode(Skip.self)) == nil { break }
        }
    }
}

/// 通知：已按控制台、oMLX、macOS 版本筛过，去掉了关掉的、过期的
struct Notice: Decodable, Identifiable {
    let id: String
    var level: String?      // info warn critical
    var title: String?
    var body: String?
    var url: String?
}

/// updates show / check 的回复
struct UpdatesView: Decodable {
    struct Check: Decodable { var enabled: Bool?; var configured: Bool?; var last: String?; var last_ok: Bool?; var error: String?; var source: String?; var text: String? }
    struct ConsoleRow: Decodable { var version: String?; var latest: String?; var state: String?; var state_text: String?; var url: String?; var notes: String? }
    struct Target: Decodable { var version: String; var state: String?; var state_text: String? }
    struct EngineRow: Decodable { var kind: String?; var version: String?; var state: String?; var state_text: String?; var target: Target?; var note: String? }
    var ok = false
    var check: Check?
    var console: ConsoleRow?
    var omlx: EngineRow?
    var notices: [Notice] = []
    var error: String?

    private enum Field: String, CodingKey { case ok, check, console, omlx, notices, error }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Field.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        check = try? c.decode(Check.self, forKey: .check)
        console = try? c.decode(ConsoleRow.self, forKey: .console)
        omlx = try? c.decode(EngineRow.self, forKey: .omlx)
        notices = (try? c.decode(LenientList<Notice>.self, forKey: .notices))?.items ?? []
        error = try? c.decode(String.self, forKey: .error)
    }
}

/// engine install-plan 的回复
struct InstallPlan: Decodable {
    struct Target: Decodable { var version: String; var state: String?; var state_text: String?; var file: String?; var size: Int64?; var size_text: String?; var only_tested_on: String? }
    struct Source: Decodable, Identifiable { var id: String; var name: String? }
    struct Brew: Decodable { var installed: Bool?; var commands: [String]?; var start: String?; var note: String? }
    struct Downloaded: Decodable { var path: String; var version: String? }
    var ok: Bool
    var kind: String?
    var macos: String?
    var system: String?
    var target: Target?
    var reason: String?
    var sources: [Source]?
    var source_pref: String?
    var brew: Brew?
    var downloaded: Downloaded?
    var apps_dir: String?
    var error: String?
}

struct FileCheckResult: Decodable { var ok: Bool; var match: Bool?; var usable: Bool?; var version: String?; var state: String?; var state_text: String?; var system: String?; var fits_system: Bool?; var message: String?; var error: String? }
struct DownloadResult: Decodable { var ok: Bool; var path: String?; var version: String?; var resumed: Bool?; var error: String? }
struct PackageResult: Decodable { var ok: Bool; var steps: [String]?; var error: String? }
struct BackupResult: Decodable { var ok: Bool; var path: String?; var error: String? }
struct SelfCheckResult: Decodable {
    struct Item: Decodable { var name: String; var ok: Bool; var detail: String? }
    var ok: Bool
    var passed: Bool?
    var items: [Item]?
    var error: String?
}
/// oMLX 自己的自动更新（它的 update-prefs.json）
struct AutoUpdateInfo: Decodable { var ok: Bool; var applies: Bool?; var autoCheck: Bool?; var backgroundDownload: Bool?; var ours: Bool?; var can_restore: Bool?; var app_running: Bool?; var note: String?; var error: String? }
/// 下载源三档（10.8）
struct SourceInfo: Decodable { var ok: Bool; var pref: String?; var pref_text: String?; var hf_endpoint: String?; var cn_endpoint: String?; var mirror: Bool?; var mirror_about: String?; var changed_by_us: Bool?; var can_restore: Bool?; var applies: Bool?; var note: String?; var error: String? }
/// 长命令的协议行
private struct ProgressLine: Decodable { var done: Int64?; var total: Int64?; var speed: Double? }
private struct SourceLine: Decodable { var id: String?; var name: String?; var why: String? }
private struct ErrorReply: Decodable { var ok: Bool?; var error: String? }

/// 命令的最后一个 JSON 对象（--json 时后端最后一行打它，前面可能有说明行）
func lastJSON<T: Decodable>(_ text: String, as type: T.Type) -> T? {
    for line in text.split(separator: "\n").reversed() {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard t.hasPrefix("{") else { continue }
        return try? JSONDecoder().decode(T.self, from: Data(t.utf8))
    }
    return nil
}

/// 失败时给人看的话：后端的 {"ok": false, "error"}，没有就看错误输出的最后一行
func failureText(_ r: (code: Int32, out: String, err: String)) -> String {
    if let e = lastJSON(r.out, as: ErrorReply.self)?.error, !e.isEmpty { return e }
    let err = r.err.split(separator: "\n").last.map { $0.trimmingCharacters(in: .whitespaces) } ?? ""
    return err.isEmpty ? L("操作失败（退出码 \(r.code)）") : err.replacingOccurrences(of: "✕ ", with: "")
}

func bytesText(_ n: Int64) -> String { ByteCountFormatter.string(fromByteCount: n, countStyle: .file) }

/// 安装、升级流程走到哪了。放在 Console 里：窗口关了再开，下载还在跑、进度还在
struct SetupState {
    enum Mode: Equatable { case install, upgrade(String) }
    enum Stage { case choose, ready, opened, installed }
    var mode: Mode = .install
    var stage: Stage = .choose
    var active = false              // 这次运行打开过这个流程：装好以后才接着做"装好以后"那几步
    var plan: InstallPlan?
    var planError: String?
    var busy: String?               // 正在下载、核对、备份、自检时的说明；nil 是空闲
    var stoppable = false
    var done: Int64 = 0
    var total: Int64 = 0
    var speed: Double = 0
    var sourceWhy: String?
    var pickedSource = "auto"       // auto：按设置里的下载源
    var file: String?               // 核对过、能打开的安装包
    var fileVersion: String?
    var message: String?            // 最近一步的结果
    var failed = false
    var steps: [String] = []        // open-package 给的步骤
    var backup: String?             // 升级前把设置备份到哪
    var selfcheck: SelfCheckResult?
    var afterInstall: [String] = [] // 装好以后几步的结果
    var cancelled = false
}

extension Console {
    // MARK: 更新中心（updates show / check）
    enum UpdatesWhen { case launch, regular, force }

    var visibleNotices: [Notice] { Backend.isManager ? [] : updates?.notices ?? [] }

    /// 控制台有新版：主界面一条横幅；点 ✕ 后这一版不再提示
    var consoleUpdate: UpdatesView.ConsoleRow? {
        guard !Backend.isManager, let row = updates?.console, row.state == "update", let latest = row.latest,
              UserDefaults.standard.string(forKey: "hiddenConsoleUpdate") != latest else { return nil }
        return row
    }

    func hideConsoleUpdate() {
        UserDefaults.standard.set(updates?.console?.latest, forKey: "hiddenConsoleUpdate")
        objectWillChange.send()
    }

    /// 启动时读一次（后端：距上次一小时以上才真的联网），之后每 6 小时一次（后端：24 小时以上才联网）
    func startUpdates() {
        guard !updatesStarted, !Backend.isManager, !Backend.isPreview else { return }
        updatesStarted = true
        checkUpdates(.launch)
    }

    func checkUpdates(_ when: UpdatesWhen) {
        guard !Backend.isManager, !Backend.isPreview, !updatesChecking else { return }
        updatesChecking = true
        lastUpdatesCheck = Date()
        let args = ["updates", "check"] + (when == .launch ? ["launch"] : when == .force ? ["force"] : []) + ["--json"]
        DispatchQueue.global().async {
            let r = Backend.run(args)
            let view = lastJSON(r.out, as: UpdatesView.self)
            DispatchQueue.main.async {
                self.updatesChecking = false
                if let v = view, v.ok { self.updates = v; self.updatesError = nil }
                else if when == .force { self.updatesError = failureText(r) }   // 后台的检查没成不打扰，更新页"现在检查"才说
            }
        }
    }

    /// updates show / enable / disable / dismiss：回复都是 updates show
    func updatesCommand(_ args: [String]) {
        guard !Backend.isManager, !Backend.isPreview else { return }
        DispatchQueue.global().async {
            let r = Backend.run(["updates"] + args + ["--json"])
            let view = lastJSON(r.out, as: UpdatesView.self)
            DispatchQueue.main.async {
                if let v = view, v.ok { self.updates = v; self.updatesError = nil } else { self.updatesError = failureText(r) }
            }
        }
    }

    func setUpdatesEnabled(_ on: Bool) { updatesCommand([on ? "enable" : "disable"]) }
    func dismissNotice(_ id: String) {
        updates?.notices.removeAll { $0.id == id }   // 先在界面上去掉，后端记下
        updatesCommand(["dismiss", id])
    }

    /// 更新页打开时读一次：oMLX 自己的自动更新、下载源、更新中心
    func loadUpdatesPage() {
        guard !Backend.isManager, !Backend.isPreview else { return }
        DispatchQueue.global().async {
            let au = lastJSON(Backend.run(["engine", "autoupdate", "show", "--json"]).out, as: AutoUpdateInfo.self)
            let src = Backend.run(["engine", "source", "show", "--json"])
            let source = lastJSON(src.out, as: SourceInfo.self)
            let up = lastJSON(Backend.run(["updates", "show", "--json"]).out, as: UpdatesView.self)
            DispatchQueue.main.async {
                if let au, au.ok { self.autoUpdate = au }
                if let source, source.ok { self.sourceInfo = source; self.extrasError = nil } else { self.extrasError = failureText(src) }
                if let up, up.ok { self.updates = up }
            }
        }
    }

    // MARK: 下载源（10.8）
    /// 已定第 29 条 ①：选「中国」只是魔搭优先，不改 oMLX 的设置，所以不用确认；社区镜像要用户自己勾，勾之前说清
    func setSource(_ pref: String) {
        guard pref != (sourceInfo?.pref ?? "auto") else { return }
        runSource([pref])
    }

    func setMirror(_ on: Bool) {
        guard on != (sourceInfo?.mirror ?? false) else { return }
        if on {
            let mirror = sourceInfo?.cn_endpoint ?? "hf-mirror.com"
            guard confirm(L("把 oMLX 的 Hugging Face 地址换成社区镜像？"),
                          L("\(mirror) 是个人维护的公益镜像，没有官方保障；它同时给文件和校验值，出了错核不出来。在中国下载模型，建议先在 oMLX 的模型下载页选「魔搭」，魔搭上没有的再用它。\n\n随时能取消勾选，或在「更新」页一键还原。"),
                          ok: L("换成社区镜像")) else { objectWillChange.send(); return }   // 取消：勾选框回到没勾
        }
        runSource(on ? ["cn", "mirror"] : ["cn"])
    }

    func restoreSource() { runSource(["restore"]) }

    private func runSource(_ args: [String]) {
        DispatchQueue.global().async {
            let r = Backend.run(["engine", "source"] + args + ["--json"])
            let s = lastJSON(r.out, as: SourceInfo.self)
            DispatchQueue.main.async {
                if let s, s.ok {
                    self.sourceInfo = s
                    self.extrasError = nil
                    self.append("▶ " + L("下载源：\(s.pref_text ?? s.pref ?? "")"), false)
                    if let note = s.note { self.append(note, false) }
                } else {
                    self.extrasError = failureText(r)
                    self.objectWillChange.send()
                }
            }
        }
    }

    // MARK: oMLX 自己的自动更新
    func confirmAutoUpdateOff() -> Bool {
        confirm(L("关掉 oMLX 自己的自动更新？"),
                L("oMLX 默认会在后台下载约 900 MB 的新安装包，这些版本没经过控制台核对。关掉以后，有新版时由控制台的「更新」页提示，核对过再装。\n\n随时能在「更新」页还原。"),
                ok: L("关掉"))
    }

    func askAutoUpdateOff() { if confirmAutoUpdateOff() { setAutoUpdate(off: true) } }

    func setAutoUpdate(off: Bool, then: (() -> Void)? = nil) {
        DispatchQueue.global().async {
            let r = Backend.run(["engine", "autoupdate", off ? "off" : "restore", "--json"])
            let au = lastJSON(r.out, as: AutoUpdateInfo.self)
            DispatchQueue.main.async {
                let label = off ? L("关掉 oMLX 自己的自动更新") : L("还原 oMLX 自己的自动更新")
                if let au, au.ok {
                    self.autoUpdate = au
                    self.append("▶ " + label, false)
                    if let note = au.note { self.append(note, false) }
                    if self.setup.stage == .installed { self.setup.afterInstall.append(label + (au.note.map { L("：\($0)") } ?? "")) }
                } else {
                    let text = failureText(r)
                    self.append("▶ " + label, false)
                    self.append(text, true)
                    if self.setup.stage == .installed { self.setup.afterInstall.append(L("\(label)没成：\(text)")) }
                }
                then?()
            }
        }
    }

    // MARK: 安装和升级流程（10.5、10.6）
    func openSetup(_ mode: SetupState.Mode, open: OpenWindowAction?) {
        if setup.busy == nil && setup.mode != mode { setup = SetupState(mode: mode) }   // 换了才清；正在做的不打断
        setup.active = true
        if setup.plan == nil || setup.busy == nil { loadPlan() }
        (open ?? openWindowAction)?(id: "omlx-setup")
        NSApp.activate(ignoringOtherApps: true)
    }

    func loadPlan() {
        let mode = setup.mode   // 在主线程取：后台线程不读界面状态（体检 A15）
        DispatchQueue.global().async {
            var args = ["engine", "install-plan"]
            if case .upgrade(let v) = mode { args.append("version=\(v)") }   // 升级时只认要升到的那版下好的
            let r = Backend.run(args + ["--json"])
            let plan = lastJSON(r.out, as: InstallPlan.self)
            DispatchQueue.main.async {
                guard let plan, plan.ok else { self.setup.planError = failureText(r); return }
                self.setup.plan = plan
                self.setup.planError = nil
                // 下载目录里已经有核对过的那份：直接给"打开安装包"
                guard self.setup.file == nil, self.setup.busy == nil, let d = plan.downloaded else { return }
                switch self.setup.mode {
                case .install where self.setup.stage == .choose:
                    self.setup.file = d.path; self.setup.fileVersion = d.version; self.setup.stage = .ready
                case .upgrade(let v) where d.version == v:
                    self.setup.file = d.path; self.setup.fileVersion = d.version
                default: break
                }
            }
        }
    }

    func stopSetup() {
        guard setup.stoppable, let p = setupProcess else { return }
        setup.cancelled = true
        p.terminate()
    }

    /// 长命令：@@progress、@@source 是协议行，只更新进度，不进记录；最后一行 JSON 交给 finish
    private func runSetupStream(_ label: String, _ args: [String], stopped: String, finish: @escaping (_ json: String?, _ failure: String?) -> Void) {
        setup.busy = label
        setup.stoppable = true
        setup.done = 0; setup.total = 0; setup.speed = 0
        setup.sourceWhy = nil
        setup.message = nil
        setup.failed = false
        setup.cancelled = false
        DispatchQueue.global().async {
            var last: String?
            var errors: [String] = []
            let code = Backend.stream(args, onStart: { p in DispatchQueue.main.async { self.setupProcess = p } }) { line, isError in
                if line.hasPrefix("@@progress ") {
                    if let p = try? JSONDecoder().decode(ProgressLine.self, from: Data(line.dropFirst(11).utf8)) {
                        DispatchQueue.main.async { self.setup.done = p.done ?? 0; self.setup.total = p.total ?? 0; self.setup.speed = p.speed ?? 0 }
                    }
                    return
                }
                if line.hasPrefix("@@source ") {
                    let s = try? JSONDecoder().decode(SourceLine.self, from: Data(line.dropFirst(9).utf8))
                    DispatchQueue.main.async {
                        self.setup.sourceWhy = s.map { s in L("来源：\(s.name ?? s.id ?? "")") + (s.why.map { L("（\($0)）") } ?? "") }
                    }
                    return
                }
                if line.hasPrefix("@@") { return }
                let t = line.trimmingCharacters(in: .whitespaces)
                if t.isEmpty { return }
                if isError { errors.append(t) } else if t.hasPrefix("{") { last = t }
            }
            DispatchQueue.main.async {
                self.setupProcess = nil
                // 下载、核对途中用别的办法装好了（Homebrew、手头另一份安装包）：finishInstall 已经接手，这边迟到的结果作废。
                // 以前它会把"装好了"改回"安装包准备好了"，下一次刷新再走一遍装好以后那几步（体检 A10）
                if self.setup.stage == .installed { self.setup.cancelled = false; self.setup.stoppable = false; return }
                self.setup.busy = nil
                self.setup.stoppable = false
                if self.setup.cancelled {
                    self.setup.cancelled = false
                    self.setup.message = stopped
                    return
                }
                var failure: String?
                if code != 0 {
                    let reply = last.flatMap { try? JSONDecoder().decode(ErrorReply.self, from: Data($0.utf8)) }
                    failure = reply?.error ?? errors.last?.replacingOccurrences(of: "✕ ", with: "") ?? L("\(label)没成（退出码 \(code)）")
                }
                finish(last, failure)
            }
        }
    }

    private func setupFailed(_ text: String) {
        setup.failed = true
        setup.message = text
        append(text, true)
    }

    /// 下载官方安装包：装的时候后端按兼容表挑版本，升级时指定版本；来源 auto 按设置里的下载源
    func setupDownload() {
        guard setup.busy == nil else { return }
        var args = ["engine", "download"]
        if case .upgrade(let v) = setup.mode { args.append("version=" + v) }
        if setup.pickedSource != "auto" { args.append("source=" + setup.pickedSource) }
        append("▶ " + L("下载 oMLX 安装包"), false)
        runSetupStream(L("正在下载 oMLX 安装包…"), args + ["--json"], stopped: L("已停止下载。下了的部分留着，下次接着下。")) { json, failure in
            if let failure { self.setupFailed(failure); return }
            guard let d = json.flatMap({ try? JSONDecoder().decode(DownloadResult.self, from: Data($0.utf8)) }), d.ok, let path = d.path else {
                self.setupFailed(L("下载完了，但读不懂后端的回复。")); return
            }
            self.setup.file = path
            self.setup.fileVersion = d.version
            if self.setup.mode == .install { self.setup.stage = .ready }
            self.setup.message = L("下载完了，大小和 SHA-256 都和兼容表对上：\(d.version ?? "")。")
            self.append(self.setup.message ?? "", false)
        }
    }

    /// 我已经下载好了：选 .dmg，按兼容表核对
    func setupPickFile() {
        guard setup.busy == nil else { return }
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(filenameExtension: "dmg") ?? .diskImage]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = L("选下载好的 oMLX 安装包（.dmg）")
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        runSetupStream(L("正在核对安装包…"), ["engine", "check-file", url.path, "--json"], stopped: L("已停止核对。")) { json, failure in
            if let failure { self.setupFailed(failure); return }
            guard let c = json.flatMap({ try? JSONDecoder().decode(FileCheckResult.self, from: Data($0.utf8)) }), c.ok else {
                self.setupFailed(L("核对完了，但读不懂后端的回复。")); return
            }
            if case .upgrade(let v) = self.setup.mode, c.usable == true, c.version != v {
                self.setupFailed(L("这是 \(c.version ?? L("别的版本"))，这次要升级到 \(v)。")); return
            }
            guard c.usable == true else { self.setupFailed(c.message ?? L("这个文件不能用来装。")); return }
            self.setup.file = url.path
            self.setup.fileVersion = c.version
            if self.setup.mode == .install { self.setup.stage = .ready }
            self.setup.failed = false
            self.setup.message = L("核对过：\(c.version ?? "")") + (c.state_text.map { L("，\($0)") } ?? "") + L("。")
        }
    }

    func setupOpenPackage() {
        guard let file = setup.file, setup.busy == nil else { return }
        setup.busy = L("正在打开安装包…")
        setup.stoppable = false
        setup.message = nil
        let mode = setup.mode
        DispatchQueue.global().async {
            var args = ["engine", "open-package", file]
            if case .upgrade = mode { args.append("upgrade") }   // 升级的步骤：先退出 oMLX、替换
            let r = Backend.run(args + ["--json"])
            let o = lastJSON(r.out, as: PackageResult.self)
            DispatchQueue.main.async {
                if self.setup.stage == .installed { return }   // 打开途中已经装好了（A10）
                self.setup.busy = nil
                guard let o, o.ok else { self.openPackageFailed(failureText(r)); return }
                self.setup.steps = o.steps ?? []
                self.setup.stage = .opened
                self.setup.failed = false
                self.append("▶ " + L("打开 oMLX 安装包"), false)
            }
        }
    }

    /// 打开安装包没成（文件被删了、打开前再核一次没过、兼容表更新后这一版不能用了）：这份不再用，把重新下载、另选一份的路让出来。
    /// 以前升级窗口里文件一旦选定，"开始"和"我已经下载好了…"就一直灰着，只能重启应用（体检 A11）
    private func openPackageFailed(_ text: String) {
        setup.file = nil
        setup.fileVersion = nil
        if case .upgrade = setup.mode {
            setupFailed(text + "\n" + L("这份安装包不再用。点「开始」重新下载，或点「我已经下载好了…」另选一份。"))
        } else {
            setup.stage = .choose   // 安装：回到三条路
            setupFailed(text + "\n" + L("这份安装包不再用。在下面重新下载，或另选一份。"))
        }
    }

    /// 每次读到状态：流程开着、这台从"没装"变成官方版，就接着做装好以后的三步
    func watchInstall() {
        guard setup.active, setup.mode == .install, setup.stage != .installed, engine?.kind == "official" else { return }
        finishInstall()
    }

    /// 装好以后：问要不要关 oMLX 自己的自动更新 → 启动 → 主密钥授权（3a 的 askEngineKey 自己会问）
    private func finishInstall() {
        setup.stage = .installed
        setup.busy = nil
        setup.stoppable = false
        setupProcess?.terminate()   // 还在下载或核对的那次不用了；它的回调看到"装好了"就不再动（A10）
        let version = engine?.version ?? setup.fileVersion
        setup.afterInstall = [version.map { L("oMLX 装好了：\($0)。") } ?? L("oMLX 装好了。")]
        append("▶ " + setup.afterInstall[0], false)
        setupPrompting = true
        DispatchQueue.global().async {
            // 先核官方签名（几秒）：不是官方签的 oMLX.app 不往下走，也不问主密钥
            struct Signed: Decodable { var ok: Bool; var signed: Bool? }
            let sig = lastJSON(Backend.run(["engine", "show", "--json"]).out, as: Signed.self)
            if sig?.signed == false {
                DispatchQueue.main.async {
                    self.engineUnsigned = true
                    self.setupPrompting = false
                    self.setup.afterInstall.append(L("这个 oMLX 不是官方签名的，控制台不对接它。请删掉它，从官方安装包重新装。"))
                    self.setup.failed = true
                }
                return
            }
            let au = lastJSON(Backend.run(["engine", "autoupdate", "show", "--json"]).out, as: AutoUpdateInfo.self)
            DispatchQueue.main.async {
                if let au, au.ok { self.autoUpdate = au }
                if let au, au.ok, au.applies == true, au.autoCheck == true || au.backgroundDownload == true {
                    if self.confirmAutoUpdateOff() { self.setAutoUpdate(off: true) { self.startAfterInstall() }; return }
                    self.setup.afterInstall.append(L("oMLX 自己的自动更新照旧开着，能在「更新」页关。"))
                }
                self.startAfterInstall()
            }
        }
    }

    private func startAfterInstall() {
        setupPrompting = false
        guard let key = localKey else { return }
        if node(key)?.service == "down" {
            setup.afterInstall.append(L("正在启动 oMLX…"))
            startService(key)
        }
        setup.afterInstall.append(L("接下来控制台会问你允不允许读 oMLX 的主密钥；允许以后就能加载模型了。"))
    }

    /// 升级第 1 步：先备份设置，再下载（pick 为 true 时改成选已经下好的文件）
    func upgradeStart(pick: Bool = false) {
        guard case .upgrade = setup.mode, setup.busy == nil else { return }
        let next = { if self.setup.file == nil { pick ? self.setupPickFile() : self.setupDownload() } }
        if setup.backup != nil { next(); return }
        setup.busy = L("正在备份 oMLX 的设置…")
        setup.stoppable = false
        setup.message = nil
        DispatchQueue.global().async {
            let r = Backend.run(["engine", "backup-settings", "--json"])
            let b = lastJSON(r.out, as: BackupResult.self)
            DispatchQueue.main.async {
                self.setup.busy = nil
                guard let b, b.ok, let path = b.path else { self.setupFailed(failureText(r)); return }
                self.setup.backup = path
                self.append("▶ " + L("备份 oMLX 的设置：\(path)"), false)
                next()
            }
        }
    }

    /// 升级第 3 步：用户换好了，自检
    func upgradeSelfcheck() {
        guard case .upgrade(let v) = setup.mode, setup.busy == nil else { return }
        setup.busy = L("正在自检…")
        setup.stoppable = false
        setup.message = nil
        DispatchQueue.global().async {
            let r = Backend.run(["engine", "selfcheck", "expect=" + v, "--json"])
            let s = lastJSON(r.out, as: SelfCheckResult.self)
            DispatchQueue.main.async {
                self.setup.busy = nil
                guard let s, s.ok else { self.setupFailed(failureText(r)); return }
                self.setup.selfcheck = s
                self.append("▶ " + (s.passed == true ? L("升级后自检：全部通过") : L("升级后自检：有没过的项")), s.passed != true)
                self.refresh(full: true)
                self.updatesCommand(["show"])
            }
        }
    }
}

/// 一条通知：critical、warn 橙色，info 普通；✕ 后不再显示
struct NoticeBanner: View {
    @EnvironmentObject var console: Console
    let notice: Notice

    private var strong: Bool { ["warn", "critical"].contains(notice.level ?? "") }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: notice.level == "critical" ? "exclamationmark.triangle.fill" : strong ? "exclamationmark.circle.fill" : "info.circle.fill")
                .foregroundStyle(strong ? Color.orange : Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                if let title = notice.title { Text(title).font(.callout.bold()) }
                if let body = notice.body { Text(body).font(.callout).fixedSize(horizontal: false, vertical: true) }
            }
            Spacer(minLength: 8)
            if let link = notice.url.flatMap({ URL(string: $0) }) { Link(L("详情"), destination: link).controlSize(.small) }
            Button { console.dismissNotice(notice.id) } label: { Image(systemName: "xmark") }
                .buttonStyle(.borderless).help(L("不再显示这条"))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill((strong ? Color.orange : Color.accentColor).opacity(0.12)))
    }
}

/// 控制台有新版：一行，加下载页的链接
struct ConsoleUpdateBanner: View {
    @EnvironmentObject var console: Console
    let row: UpdatesView.ConsoleRow

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "arrow.down.circle.fill").foregroundStyle(Color.accentColor)
            Text(L("控制台有新版 \(row.latest ?? "")") + (row.notes.map { L("：\($0)") } ?? "")).font(.callout).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if let link = row.url.flatMap({ URL(string: $0) }) { Link(L("下载页"), destination: link).controlSize(.small) }
            Button { console.hideConsoleUpdate() } label: { Image(systemName: "xmark") }
                .buttonStyle(.borderless).help(L("这一版不再提示"))
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.accentColor.opacity(0.12)))
    }
}

/// 本机卡片上的"开始使用"（第 4 期，草案 11.2）：三步各一行，做完的打勾，轮到的那一步给说明和一个按钮
struct SetupGuide: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow
    let key: String
    let info: SetupInfo
    @State private var picked = ""   // A3：第 3 步挑中的模型；空着就用放得下的第一个

    /// A3（已定第 31 条 ④）：这台已有的全部模型，标大小；加载要的内存超过安全线的标"放不下"（照样能选，加载时后端会再拦）
    private var choices: [(m: String, text: String, fits: Bool)] {
        let n = console.node(key)
        return console.models(on: key).map { m in
            let need = n?.need_gb?[m]
            let fits = need.flatMap { g in n?.line_gb.map { g <= $0 } } ?? true
            return (m, console.title(m, on: key) + (need.map { " · \($0) GB" } ?? "") + (fits ? "" : L(" · 放不下")), fits)
        }
    }
    private var chosen: String? {
        let all = choices
        return all.first { $0.m == picked }?.m ?? all.first { $0.fits }?.m ?? all.first?.m
    }

    private func step(_ id: String) -> SetupStep? { info.steps.first { $0.id == id } }

    private func row<Action: View>(_ number: Int, _ s: SetupStep?, _ title: String, _ detail: String, @ViewBuilder action: () -> Action) -> some View {
        let current = s != nil && info.next == s?.id
        return HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: s?.done == true ? "checkmark.circle.fill" : "\(number).circle")
                .foregroundStyle(s?.done == true ? Color.green : current ? Color.accentColor : Color.secondary)
            VStack(alignment: .leading, spacing: 5) {
                Text(title).fontWeight(current ? .medium : .regular).foregroundStyle(s?.done == true || current ? .primary : .secondary)
                if current {
                    if !detail.isEmpty { Text(detail).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                    action()
                }
            }
        }
    }

    private var engineTitle: String {
        guard let s = step("engine") else { return "" }
        return s.done ? L("oMLX 装好了，在运行") : s.state == "missing" ? L("安装 oMLX") : L("启动 oMLX")
    }

    private var engineDetail: String {
        step("engine")?.state == "missing" ? L("控制台用 oMLX 运行模型：这里帮你装官方版，装好后自动接上。") : L("oMLX 装好了，还没启动。启动不会自动加载模型。")
    }

    private var modelTitle: String {
        guard let s = step("model") else { return "" }
        return s.done ? L("这台有 \(s.count ?? 0) 个模型") : L("下载一个模型")
    }

    private var modelDetail: String {
        guard let s = step("model") else { return "" }
        if s.need_key == true { return L("先允许控制台读 oMLX 的主密钥，才看得到这台有哪些模型。") }
        var text = L("模型在 oMLX 的管理页里下载，下完回到这里，卡片会自己出现。")
        if let memory = s.memory_gb, let suggest = s.suggest_gb { text += L("这台内存 \(memory) GB，建议挑 \(suggest) GB 以内的 MLX 格式模型。") }
        if s.china == true { text += L("下载源选了“中国”：在 oMLX 的下载页里选魔搭。") }
        return text
    }

    var body: some View {
        let engine = step("engine"), model = step("model"), load = step("load")
        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(L("开始使用")).font(.headline)
                Spacer()
                if info.done { Button(L("收起")) { console.showSetupAgain = false }.controlSize(.small) }
            }
            row(1, engine, engineTitle, engineDetail) {
                if engine?.state == "missing" {
                    Button(L("安装 oMLX…")) { console.openSetup(.install, open: openWindow) }.buttonStyle(.borderedProminent)
                } else {
                    Button(L("启动 oMLX")) { console.startService(key) }.buttonStyle(.borderedProminent).disabled(console.busy)
                }
            }
            row(2, model, modelTitle, modelDetail) {
                if model?.need_key == true {
                    Button(L("允许读主密钥…")) { console.askEngineKeyNow() }.buttonStyle(.borderedProminent)
                } else {
                    HStack(spacing: 8) {
                        Button(L("打开 oMLX 的管理页")) { console.openAdmin(key) }.buttonStyle(.borderedProminent)
                        Button(L("添加已有的模型文件夹…")) { console.addModelsFolder() }
                            .disabled(console.busy || engine?.done != true)
                            .help(L("硬盘上已经有 MLX 格式的模型：选它们所在的文件夹，oMLX 会认出来，不用重新下载"))
                    }
                }
            }
            row(3, load, load?.done == true ? L("加载并自检过了") : L("加载并自检"), L("加载完控制台会让模型算一道题，确认它能正常回答。")) {
                let all = choices
                if let m = chosen {
                    HStack(spacing: 8) {
                        if all.count > 1 {
                            Picker("", selection: Binding(get: { m }, set: { picked = $0 })) {
                                ForEach(all, id: \.m) { Text($0.text).tag($0.m) }
                            }
                            .labelsHidden().pickerStyle(.menu).fixedSize()
                        } else if let only = all.first {
                            Text(only.text).foregroundStyle(only.fits ? Color.primary : Color.orange)
                        }
                        Button(L("加载并自检")) { console.load(m, on: key) }.buttonStyle(.borderedProminent).disabled(console.busy)
                    }
                    if all.first(where: { $0.m == m })?.fits == false {
                        Text(L("这个模型比这台的安全线还大，多半加载不了。挑一个小一点的，或在 oMLX 的管理页里下载一个放得下的。"))
                            .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.accentColor.opacity(0.07)))
    }
}

/// 本机卡片：这台没装 oMLX 时的说明和入口
struct InstallHint: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(L("这台还没装 oMLX。控制台用它运行模型：这里帮你装官方版，装好后自动接上。"))
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Button(L("安装 oMLX…")) { console.openSetup(.install, open: openWindow) }
                .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, minHeight: 90, alignment: .leading)
    }
}

/// 设置的"更新"页：控制台、oMLX 各一行，联网检查，下载源
struct UpdatesSettings: View {
    @EnvironmentObject var console: Console
    @Environment(\.openWindow) private var openWindow

    private var u: UpdatesView? { console.updates }

    private func shortTime(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: iso) else { return iso }
        let f = DateFormatter()
        f.dateFormat = "MM-dd HH:mm"
        return f.string(from: date)
    }

    private var checkText: String {
        if let text = u?.check?.text { return text }
        if let last = u?.check?.last { return L("上次检查：\(shortTime(last))") }
        return u == nil ? L("读取中…") : L("还没检查过")
    }

    private func autoText(_ au: AutoUpdateInfo) -> String {
        if au.backgroundDownload == true { return L("开着：会在后台下载新安装包") }
        if au.autoCheck == true { return L("开着：只检查，不下载") }
        return au.ours == true ? L("已由控制台关掉") : L("关着")
    }

    @ViewBuilder private var engineRows: some View {
        let row = u?.omlx
        if let v = row?.version { LabeledContent(L("版本"), value: v) }
        LabeledContent(L("状态")) {
            Text(row?.state_text ?? L("读取中…"))
                .foregroundStyle(["bad", "old", "unknown"].contains(row?.state ?? "") ? Color.orange : Color.secondary)
        }
        if let note = row?.note { Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
        if row?.kind == "official", let t = row?.target {
            HStack(spacing: 8) {
                Text(L("可以升级到 \(t.version)") + (t.state_text.map { L("（\($0)）") } ?? "")).foregroundStyle(.secondary)
                Spacer(minLength: 8)
                Button(L("升级…")) { console.openSetup(.upgrade(t.version), open: openWindow) }
            }
        }
        if row?.kind == "none" { Button(L("安装 oMLX…")) { console.openSetup(.install, open: openWindow) } }
        if let au = console.autoUpdate, au.applies == true {
            LabeledContent(L("oMLX 自己的自动更新")) {
                HStack(spacing: 8) {
                    Text(autoText(au)).foregroundStyle(.secondary)
                    if au.autoCheck == true || au.backgroundDownload == true { Button(L("关掉…")) { console.askAutoUpdateOff() } }
                    else if au.can_restore == true { Button(L("还原")) { console.setAutoUpdate(off: false) } }
                }
            }
            if let note = au.note { Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
        }
    }

    var body: some View {
        Form {
            Section(L("控制台")) {
                LabeledContent(L("版本"), value: u?.console?.version ?? Backend.version)
                LabeledContent(L("状态")) {
                    HStack(spacing: 8) {
                        Text(u?.console?.state_text ?? L("没检查过"))
                            .foregroundStyle(u?.console?.state == "update" ? Color.accentColor : Color.secondary)
                        if u?.console?.state == "update", let link = u?.console?.url.flatMap({ URL(string: $0) }) { Link(L("下载页"), destination: link) }
                    }
                }
                if u?.console?.state == "update", let notes = u?.console?.notes {
                    Text(notes).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            Section { engineRows } header: { Text(verbatim: "oMLX") }
            Section(L("联网检查")) {
                Toggle(L("联网检查更新和通知（每天一次）"), isOn: Binding(get: { u?.check?.enabled ?? true }, set: { console.setUpdatesEnabled($0) }))
                    .disabled(u == nil)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(checkText).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    if console.updatesChecking { ProgressView().controlSize(.small) }
                    Button(L("现在检查")) { console.checkUpdates(.force) }
                        .disabled(console.updatesChecking || u?.check?.configured != true || u?.check?.enabled == false)
                }
                if let error = console.updatesError ?? u?.check?.error {
                    Text(L("上次没读成：\(error)")).font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                }
                Text(L("只读两样小文件：兼容表和通知清单，都用发布者的钥匙签了名，被改过就不认。不上传任何东西。"))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Section(L("下载源")) {
                Picker(L("下载源"), selection: Binding(get: { console.sourceInfo?.pref ?? "auto" }, set: { console.setSource($0) })) {
                    Text(L("自动")).tag("auto")
                    Text(L("国际")).tag("intl")
                    Text(L("中国")).tag("cn")
                }
                .disabled(console.sourceInfo == nil)
                Text(L("自动：下载 oMLX 安装包时测速，挑 GitHub 和魔搭转存里快的；国际：只用 GitHub；中国：优先魔搭转存，下载模型请在 oMLX 的模型下载页选「魔搭」（官方 oMLX 自带）。"))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Text(L("GitHub 是 oMLX 官方发布页；魔搭转存是本项目开发者原样转存的同一份安装包，供中国大陆下载。"))
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if console.sourceInfo?.pref == "cn" && console.sourceInfo?.applies == true {
                    Toggle(L("也把 oMLX 的 Hugging Face 地址换成社区镜像 \(console.sourceInfo?.cn_endpoint ?? "hf-mirror.com")"),
                           isOn: Binding(get: { console.sourceInfo?.mirror ?? false }, set: { console.setMirror($0) }))
                        .disabled(console.sourceInfo?.mirror == nil)   // 读不到 oMLX 现在的地址：不知道是不是镜像，先别让勾
                    Text(console.sourceInfo?.mirror_about ?? L("个人维护的社区镜像，没有官方保障。"))
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if console.sourceInfo?.mirror == nil {
                        Text(L("要开着 oMLX、允许控制台读主密钥，才能看到和改这一项。")).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    }
                }
                if let note = console.sourceInfo?.note { Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                if console.sourceInfo?.can_restore == true { Button(L("还原 Hugging Face 地址")) { console.restoreSource() } }
                if let e = console.extrasError { Text(e).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            }
        }
        .formStyle(.grouped)
        .onAppear { console.loadUpdatesPage() }
    }
}

/// 安装和升级窗口：装（这台没装 oMLX）或升级（官方版、兼容表里有更好的版本）
struct EngineSetupView: View {
    @EnvironmentObject var console: Console

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                switch console.setup.mode {
                case .install: InstallFlow()
                case .upgrade(let v): UpgradeFlow(target: v)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minWidth: 560, idealWidth: 620, minHeight: 480, idealHeight: 660)
        .onAppear { if console.setup.plan == nil { console.loadPlan() } }
    }
}

/// 正在做什么、进度、结果（安装和升级共用）
struct SetupStatus: View {
    @EnvironmentObject var console: Console

    private func progressText(_ s: SetupState) -> String {
        bytesText(s.done) + " / " + bytesText(s.total) + (s.speed > 0 ? " · " + bytesText(Int64(s.speed)) + "/s" : "")
    }

    var body: some View {
        let s = console.setup
        VStack(alignment: .leading, spacing: 6) {
            if let busy = s.busy {
                HStack(spacing: 8) {
                    Text(busy).fontWeight(.medium)
                    Spacer()
                    if s.stoppable { Button(L("停止")) { console.stopSetup() } }
                }
                if s.total > 0 {
                    ProgressView(value: Double(min(s.done, s.total)), total: Double(s.total))
                    Text(progressText(s)).font(.callout).monospacedDigit().foregroundStyle(.secondary)
                } else {
                    ProgressView().progressViewStyle(.linear)
                }
                if let why = s.sourceWhy { Text(why).font(.callout).foregroundStyle(.secondary) }
            }
            if let m = s.message {
                Text(m).foregroundStyle(s.failed ? Color.red : Color.secondary).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
            }
        }
    }
}

/// 一行命令加复制按钮（Homebrew 那条路）
struct CommandRow: View {
    let command: String

    var body: some View {
        HStack(spacing: 8) {
            Text(verbatim: command).font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
            Button(L("复制")) {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(command, forType: .string)
            }
            .controlSize(.small)
        }
    }
}

private func systemWord(_ slot: String) -> String {
    slot.hasPrefix("macos") ? "macOS " + slot.dropFirst(5) : slot
}

/// 装 oMLX：三条路（下载官方安装包、Homebrew、我已经下载好了）→ 打开安装包 → 等装好 → 装好以后三步
struct InstallFlow: View {
    @EnvironmentObject var console: Console

    private var s: SetupState { console.setup }
    private var plan: InstallPlan? { s.plan }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(L("安装 oMLX")).font(.title2.bold())
            Text(L("这台还没装 oMLX。控制台用它运行模型：这里帮你装官方版，装好后自动接上。"))
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let error = s.planError { Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true) }
            if let plan { target(plan) } else if s.planError == nil { ProgressView().controlSize(.small) }
            switch s.stage {
            case .choose, .ready:
                if s.stage == .ready { ready }
                choices
            case .opened: waiting
            case .installed: installed
            }
            SetupStatus()
        }
    }

    @ViewBuilder private func target(_ plan: InstallPlan) -> some View {
        if let t = plan.target {
            let size = t.size_text ?? t.size.map(bytesText)
            Text(L("要装的版本：\(t.version)") + (t.state_text.map { L("（\($0)）") } ?? "") + (size.map { L("，安装包 \($0)") } ?? ""))
                .fixedSize(horizontal: false, vertical: true)
            if let only = t.only_tested_on {
                Text(L("这一份只在 \(systemWord(only)) 上验证过。")).font(.callout).foregroundStyle(.orange)
            }
        } else {
            Text(plan.reason ?? L("兼容表里没有能装在这台上的版本。")).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
        }
    }

    private var ready: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                Text(L("核对过：\(s.fileVersion ?? "")，可以装。")).fontWeight(.medium)
                if let file = s.file { Text(verbatim: file).font(.caption).foregroundStyle(.secondary).textSelection(.enabled).lineLimit(2) }
                Button(L("打开安装包")) { console.setupOpenPackage() }.buttonStyle(.borderedProminent).disabled(s.busy != nil)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label(L("安装包准备好了"), systemImage: "checkmark.seal") }
    }

    /// 要装的那一版在 oMLX 官方 GitHub 上的发布页（标签是 v + 版本号）；读不到版本就是发布列表
    private var releasePage: URL {
        let all = URL(string: "https://github.com/jundot/omlx/releases")!
        guard let v = plan?.target?.version, v.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "." }) else { return all }
        return URL(string: "https://github.com/jundot/omlx/releases/tag/v\(v)") ?? all
    }

    /// 每个来源一句话说清是什么：GitHub 是 oMLX 官方的，魔搭是本项目开发者转存给中国大陆用的
    static func sourceLabel(_ source: InstallPlan.Source) -> String {
        switch source.id {
        case "github": return L("GitHub：oMLX 官方发布页")
        case "modelscope": return L("魔搭：本项目开发者的原样转存，供中国大陆下载")
        default: return source.name ?? source.id
        }
    }

    /// 选中的来源：没手选过时，照设置里的下载源——中国是魔搭、国际是 GitHub、自动是"自动"那一行
    private func sourceChoice(_ sources: [InstallPlan.Source]) -> Binding<String> {
        Binding(get: {
            let picked = console.setup.pickedSource
            if picked != "auto", sources.contains(where: { $0.id == picked }) { return picked }
            switch plan?.source_pref {
            case "cn" where sources.contains { $0.id == "modelscope" }: return "modelscope"
            case "intl" where sources.contains { $0.id == "github" }: return "github"
            case "cn", "intl": return sources.first?.id ?? "auto"
            default: return "auto"
            }
        }, set: { console.setup.pickedSource = $0 })
    }

    private var choices: some View {
        VStack(alignment: .leading, spacing: 12) {
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    Text((plan?.sources?.count ?? 0) > 1 ? L("两个来源是同一份 oMLX 官方安装包。下完按兼容表核对大小和 SHA-256；对不上就删掉，不打开。")
                                                         : L("下完按兼容表核对大小和 SHA-256；对不上就删掉，不打开。"))
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    if let sources = plan?.sources, sources.count > 1 {
                        // 0.5.2（用户 11-22 定）：两个来源明着列出来选，各写清是什么；设置里是"自动"才多一行自动
                        Text(L("从哪里下载")).font(.callout.weight(.medium))
                        Picker("", selection: sourceChoice(sources)) {
                            if plan?.source_pref != "cn" && plan?.source_pref != "intl" { Text(L("自动：下载时测速，挑快的那个")).tag("auto") }
                            ForEach(sources) { Text(Self.sourceLabel($0)).tag($0.id) }
                        }
                        .pickerStyle(.radioGroup).labelsHidden()
                    } else if let only = plan?.sources?.first {
                        // A2（已定第 31 条 ②）：只有一个来源时也写出来
                        Text(L("来源：\(only.name ?? only.id)") + (only.id == "github" ? L("（oMLX 官方发布页）") : only.id == "modelscope" ? L("（本项目开发者的原样转存）") : "")).font(.callout)
                    }
                    if plan?.source_pref == "cn", plan?.sources?.contains(where: { $0.id == "modelscope" }) == false, plan?.target != nil {
                        Text(L("下载源选了「中国」，但魔搭上还没有这一版的转存，只能从 GitHub 下。"))
                            .font(.callout).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
                    }
                    Button(L("下载")) { console.setupDownload() }
                        .buttonStyle(.borderedProminent).disabled(s.busy != nil || plan?.target == nil)
                    HStack(spacing: 4) {
                        Text(L("下不动？到官方发布页手动下载，再用下面的「我已经下载好了」。")).font(.callout).foregroundStyle(.secondary)
                        Link(L("打开发布页"), destination: releasePage)
                            .font(.callout)
                    }
                    .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label(L("下载官方安装包（推荐）"), systemImage: "arrow.down.circle") }
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    if plan?.brew?.installed == false {
                        Text(L("这台没装 Homebrew。要走这条路，先到 brew.sh 装好 Homebrew。")).font(.callout).foregroundStyle(.secondary)
                    }
                    ForEach(Array((plan?.brew?.commands ?? []).enumerated()), id: \.offset) { CommandRow(command: $0.element) }
                    if let start = plan?.brew?.start {
                        Text(L("装好后启动服务：")).font(.callout).foregroundStyle(.secondary)
                        CommandRow(command: start)
                    }
                    if let note = plan?.brew?.note { Text(note).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                    Text(L("在「终端」里运行。装好后控制台自动接上。")).font(.callout).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label(L("用 Homebrew 装"), systemImage: "terminal") }
            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    Text(L("已经从 GitHub 或别处下好了安装包（.dmg），选它来核对；不是适配的版本会说清。"))
                        .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                    Button(L("选择安装包…")) { console.setupPickFile() }.disabled(s.busy != nil)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            } label: { Label(L("我已经下载好了"), systemImage: "doc") }
        }
    }

    private var waiting: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(s.steps.enumerated()), id: \.offset) { Text(verbatim: "\($0.offset + 1). \($0.element)").fixedSize(horizontal: false, vertical: true) }
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(L("等你装好。装好后控制台会自己接上，不用回来点。")).foregroundStyle(.secondary)
                }
                Button(L("重新打开安装包")) { console.setupOpenPackage() }.disabled(s.busy != nil || s.file == nil)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label(L("把 oMLX 拖进「应用程序」"), systemImage: "square.and.arrow.down") }
    }

    private var installed: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 6) {
                ForEach(Array(s.afterInstall.enumerated()), id: \.offset) { Text(verbatim: "· " + $0.element).fixedSize(horizontal: false, vertical: true) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label(L("装好了"), systemImage: "checkmark.circle") }
    }
}

/// 升级 oMLX：备份设置并下载 → 退出 oMLX、替换 → 自检（10.9 e：自动换上和自动退回放 1.2.1）
struct UpgradeFlow: View {
    @EnvironmentObject var console: Console
    let target: String

    private var s: SetupState { console.setup }
    private var current: String { console.updates?.omlx?.version ?? console.engine?.version ?? L("读不到") }

    private func step<Content: View>(_ n: Int, _ title: String, done: Bool, @ViewBuilder _ content: () -> Content) -> some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) { content() }.frame(maxWidth: .infinity, alignment: .leading)
        } label: { Label(title, systemImage: done ? "checkmark.circle.fill" : "\(n).circle") }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(L("升级 oMLX")).font(.title2.bold())
            Text(L("从 \(current) 升级到 \(target)。控制台先备份 oMLX 的设置，下载并核对新版，由你替换，换好后自检一遍。"))
                .foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            step(1, L("备份设置，下载新版"), done: s.backup != nil && s.file != nil) {
                if let b = s.backup {
                    Text(L("设置备份在 \(b)（只在这台，只有你能读）。")).font(.callout).foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                if s.file != nil { Text(L("新版下好并核对过：\(s.fileVersion ?? target)。")).font(.callout).foregroundStyle(.secondary) }
                HStack(spacing: 8) {
                    Button(L("开始")) { console.upgradeStart() }
                        .buttonStyle(.borderedProminent).disabled(s.busy != nil || (s.backup != nil && s.file != nil))
                    Button(L("我已经下载好了…")) { console.upgradeStart(pick: true) }
                        .disabled(s.busy != nil || s.file != nil)
                }
            }
            step(2, L("退出 oMLX，换成新版"), done: s.stage == .opened) {
                if s.steps.isEmpty {   // 打开以后由后端给的步骤接替，免得说两遍
                    Text(L("先在菜单栏点 oMLX 的图标，选「Quit oMLX」退出；再打开安装包，把新版拖进「应用程序」替换旧版。"))
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                }
                ForEach(Array(s.steps.enumerated()), id: \.offset) { Text(verbatim: "\($0.offset + 1). \($0.element)").font(.callout).fixedSize(horizontal: false, vertical: true) }
                Button(L("打开安装包")) { console.setupOpenPackage() }.disabled(s.busy != nil || s.file == nil || s.backup == nil)
            }
            step(3, L("自检"), done: s.selfcheck?.passed == true) {
                Button(L("我换好了，自检")) { console.upgradeSelfcheck() }.disabled(s.busy != nil || s.stage != .opened)
                if let c = s.selfcheck {
                    ForEach(Array((c.items ?? []).enumerated()), id: \.offset) { pair in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Image(systemName: pair.element.ok ? "checkmark.circle.fill" : "xmark.circle.fill").foregroundStyle(pair.element.ok ? Color.green : Color.red)
                            Text(pair.element.name + (pair.element.detail.map { L("：\($0)") } ?? "")).font(.callout).fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Text(c.passed == true ? L("全部通过，升级好了。") : L("有没过的项，看上面。升级前的设置备份还在（路径见第 1 步）。"))
                        .foregroundStyle(c.passed == true ? Color.secondary : Color.red).fixedSize(horizontal: false, vertical: true)
                }
            }
            SetupStatus()
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var systemGoingDown = false

    /// 已经在退出了：这之后窗口关掉不算用户点了红色按钮
    static var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main) { [weak self] _ in
            self?.systemGoingDown = true
        }
        NotificationCenter.default.addObserver(forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { _ in AppDelegate.quitting = true }
        // A4：钉住主窗口以后，自己的别的窗口也浮起来（见 matchPinLevel）：它成为当前窗口时对一次；应用从后台回到前面时把所有窗口对一遍
        // ——应用不在最前面时新开的窗口不会成为当前窗口，只听前一个通知它就留在钉住的主窗口后面（界面验收 11-22 查出）
        NotificationCenter.default.addObserver(forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main) { note in
            if let window = note.object as? NSWindow { Console.shared?.matchPinLevel(window) }
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main) { _ in
            NSApp.windows.forEach { Console.shared?.matchPinLevel($0) }
        }
        // A1：红色按钮关掉的是主窗口时（SwiftUI 的窗口没法换代理，所以听通知）
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: nil, queue: .main) { note in
            guard let window = note.object as? NSWindow, Console.isMain(window), !(window is NSPanel) else { return }
            Console.shared?.mainWindowClosed()
        }
    }

    // 关掉窗口不退出：菜单栏图标和长条继续显示状态
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    // 点 Dock 图标：总是回到完整窗口（长条开着就收起长条）
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        Console.shared?.expandFromBar()
        return true
    }

    // 操作是应用调起的 clusterctl 子进程，应用退出后它一输出就会因管道断开而中止，所以先拦一下。
    // 默认按钮是"继续等待"，误按回车不会退出；菜单栏"退出控制台"和 ⌘Q 都走这里。
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let reply = shouldTerminate()
        // A1：是关窗口引起的退出、确认框里点了取消：窗口开回来，不让人以为控制台没了
        if reply == .terminateCancel, let console = Console.shared, console.quitFromClose { console.showMainWindow() }
        Console.shared?.quitFromClose = false
        return reply
    }

    private func shouldTerminate() -> NSApplication.TerminateReply {
        // macOS 自己关机、重启、注销时不弹窗：弹窗会把系统拦住，变成"关不了机"
        guard let console = Console.shared, !systemGoingDown, !Console.skipExitPrompt else { return .terminateNow }
        guard let label = console.running else {
            guard console.mayInterruptSetup(L("现在退出会把它打断。下了的部分留着，下次接着下。"), ok: L("仍然退出")) else { return .terminateCancel }
            return console.askExit()
        }
        let seconds = Int(Date().timeIntervalSince(console.runningSince ?? Date()))
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = L("「\(label)」还在进行中（已 \(seconds) 秒）")
        alert.informativeText = L("现在退出，这次操作可能停在半路（例如模型只加载了一半），之后要重新做一次。建议等操作记录里出现结果后再退出。")
        alert.alertStyle = .warning
        alert.addButton(withTitle: L("继续等待"))
        alert.addButton(withTitle: L("仍然退出"))
        return alert.runModal() == .alertSecondButtonReturn ? .terminateNow : .terminateCancel
    }
}

@main
struct LocalModelConsoleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var console = Console()

    var body: some Scene {
        Window(L("ModelTower"), id: "main") {
            MainGate().environmentObject(console)
        }
        .defaultSize(width: 1040, height: 720)
        .windowResizability(.contentSize)
        .windowToolbarStyle(.unifiedCompact)
        Settings {
            SettingsView().environmentObject(console)
        }
        // 第 3b 期：安装和升级 oMLX 的窗口，只在点"安装 oMLX…""升级…"时打开；不进"窗口"菜单
        Window(L("安装和升级 oMLX"), id: "omlx-setup") {
            EngineSetupView().environmentObject(console)
        }
        .defaultSize(width: 620, height: 660)
        .commandsRemoved()
        MenuBarExtra {
            MenuBarContent().environmentObject(console)
        } label: {
            Text(console.menuTitle)
        }
    }
}
