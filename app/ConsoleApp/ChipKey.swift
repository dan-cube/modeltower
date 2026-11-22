// 安全芯片确认（用户 11-04 定）：每台能做最高操作的 Mac 在安全芯片里建一把钥匙，读不出、抄不走，
// 每次签名由系统弹框要本人确认（指纹或 Mac 密码）。签出来的是 SSH 签名格式，收到操作的机器用系统自带的
// ssh-keygen -Y verify 核对，不用自己写椭圆曲线。文件里存的是只有这颗芯片解得开的钥匙包，不是钥匙本身。
import CoreGraphics
import CryptoKit
import Foundation
import LocalAuthentication

enum ChipError: LocalizedError {
    case noChip, noKey, declined, badCode, locked

    var errorDescription: String? {
        switch self {
        case .noChip: return L("这台 Mac 没有安全芯片。")
        case .noKey: return L("这台还没有登记安全芯片钥匙，或者钥匙已经失效。")
        case .declined: return L("没有确认：取消了，或者验证没通过。什么都没动。")
        case .badCode: return L("恢复码不对：应该是 24 位字母和数字。")
        case .locked: return L("这台 Mac 锁屏了，解锁后再做。")
        }
    }
}

enum Chip {
    static let namespace = "lmc-top"
    static let keyType = "ecdsa-sha2-nistp256"
    static var isAvailable: Bool { SecureEnclave.isAvailable }
    /// 锁屏时不做这些。芯片那时有时拒绝（11-04 实测 -25308），过一会儿同样锁着又能用，所以不能靠它，也不能把读不出来误判成钥匙坏了
    static var screenLocked: Bool { (CGSessionCopyCurrentDictionary() as? [String: Any])?["CGSSessionScreenIsLocked"] as? Bool ?? false }

    /// 只给测试用：换一个地方放钥匙包，不碰正式的
    static var blobOverride: String?
    static var blobPath: String { blobOverride ?? NSHomeDirectory() + "/.ssh/lmc/chip-key.blob" }

    // MARK: SSH 的线上格式

    private static func u32(_ value: Int) -> Data {
        var big = UInt32(value).bigEndian
        return Data(bytes: &big, count: 4)
    }

    private static func field(_ data: Data) -> Data { u32(data.count) + data }
    private static func field(_ text: String) -> Data { field(Data(text.utf8)) }

    /// 大整数：去掉开头多余的 0，最高位是 1 时补一个 0，否则会被当成负数
    private static func mpint(_ raw: Data) -> Data {
        var bytes = raw.drop { $0 == 0 }
        if let first = bytes.first, first & 0x80 != 0 { bytes = Data([0]) + bytes }
        return field(Data(bytes))
    }

    static func publicBlob(x963: Data) -> Data { field(keyType) + field("nistp256") + field(x963) }
    static func publicText(x963: Data) -> String { publicBlob(x963: x963).base64EncodedString() }

    /// 给人核对用的指纹和钥匙编号，和 chip.py 算法一样
    private static func keyHash(_ text: String) -> String {
        guard let blob = Data(base64Encoded: text) else { return "" }
        return SHA256.hash(data: blob).map { String(format: "%02x", $0) }.joined()
    }

    static func keyId(_ text: String) -> String { String(keyHash(text).prefix(12)) }

    static func fingerprint(publicText text: String) -> String {
        let hex = Array(keyHash(text).prefix(16).uppercased())
        return stride(from: 0, to: hex.count, by: 4).map { String(hex[$0..<min($0 + 4, hex.count)]) }.joined(separator: "-")
    }

    /// 把一段文字包成 ssh-keygen 认的待签内容，签完再包成 ASCII 的签名文件。sign 收到的是待签的原文，由它自己算 SHA-256
    static func sshSignature(_ text: String, x963: Data, sign: (Data) throws -> P256.Signing.ECDSASignature) throws -> String {
        let reserved = Data(), algorithm = "sha512"
        let digest = Data(SHA512.hash(data: Data(text.utf8)))
        let toSign = Data("SSHSIG".utf8) + field(namespace) + field(reserved) + field(algorithm) + field(digest)
        let raw = try sign(toSign).rawRepresentation
        let inner = mpint(raw.prefix(32)) + mpint(raw.suffix(32))
        let signature = field(keyType) + field(inner)
        let whole = Data("SSHSIG".utf8) + u32(1) + field(publicBlob(x963: x963)) + field(namespace) + field(reserved) + field(algorithm) + field(signature)
        let encoded = Array(whole.base64EncodedString())
        let lines = stride(from: 0, to: encoded.count, by: 70).map { String(encoded[$0..<min($0 + 70, encoded.count)]) }
        return "-----BEGIN SSH SIGNATURE-----\n" + lines.joined(separator: "\n") + "\n-----END SSH SIGNATURE-----\n"
    }

    // MARK: 这台的钥匙

    /// 选"指纹或 Mac 密码"这一档（用户 11-04 定）：没有指纹也能用，增删指纹不作废
    static func accessControl(presence: Bool = true) throws -> SecAccessControl {
        var failure: Unmanaged<CFError>?
        let flags: SecAccessControlCreateFlags = presence ? [.privateKeyUsage, .userPresence] : [.privateKeyUsage]
        guard let control = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly, flags, &failure) else {
            throw failure!.takeRetainedValue() as Error
        }
        return control
    }

    static func hasKey() -> Bool { FileManager.default.fileExists(atPath: blobPath) }

    /// 建一把新钥匙，存钥匙包，返回公钥。建的时候不弹框，第一次用的时候才弹。旧的钥匙包不删，改名留着：
    /// 万一是误判（比如读的时候芯片正忙），还能找回来
    static func createKey() throws -> String {
        guard isAvailable else { throw ChipError.noChip }
        guard !screenLocked else { throw ChipError.locked }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: accessControl())
        let directory = (blobPath as NSString).deletingLastPathComponent
        try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let temporary = blobPath + ".tmp"
        guard FileManager.default.createFile(atPath: temporary, contents: key.dataRepresentation, attributes: [.posixPermissions: 0o600]) else { throw ChipError.noKey }
        retireKey()
        try FileManager.default.moveItem(atPath: temporary, toPath: blobPath)
        return publicText(x963: key.publicKey.x963Representation)
    }

    /// 这台钥匙的公钥。读公钥不用本人确认；钥匙包坏了或换了芯片（抹盘、换主板）就读不出来
    static func publicKey() -> String? {
        guard isAvailable, let blob = FileManager.default.contents(atPath: blobPath),
              let key = try? SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob) else { return nil }
        return publicText(x963: key.publicKey.x963Representation)
    }

    /// 不再用这把钥匙：钥匙包改名留着。它离了这颗芯片没用，留着没有风险
    static func retireKey() {
        if hasKey() { try? FileManager.default.moveItem(atPath: blobPath, toPath: blobPath + ".retired-\(Int(Date().timeIntervalSince1970))") }
    }

    /// 钥匙包在，但现在读不出来，而且不是因为锁屏：抹过盘、换过主板，或者钥匙包坏了
    static var keyLost: Bool { hasKey() && !screenLocked && publicKey() == nil }

    /// 用这台的钥匙签名：系统弹框要本人确认。会阻塞，不能在主线程上调
    static func sign(_ text: String, reason: String) throws -> String {
        guard isAvailable else { throw ChipError.noChip }
        guard !screenLocked else { throw ChipError.locked }
        guard let blob = FileManager.default.contents(atPath: blobPath) else { throw ChipError.noKey }
        let context = LAContext()
        context.localizedReason = reason
        let key: SecureEnclave.P256.Signing.PrivateKey
        do { key = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob, authenticationContext: context) }
        catch { throw ChipError.noKey }
        do { return try sshSignature(text, x963: key.publicKey.x963Representation) { try key.signature(for: $0) } }
        catch { throw ChipError.declined }
    }

    // MARK: 恢复码：24 位，由它算出一把普通的钥匙。各台只存这把钥匙的公钥，码本身不存

    private static let alphabet = Array("0123456789ABCDEFGHJKMNPQRSTVWXYZ")   // 去掉 I L O U，不容易抄错

    static func newRecoveryCode() -> String {
        let characters = (0..<24).map { _ in String(alphabet[Int.random(in: 0..<32)]) }
        return stride(from: 0, to: 24, by: 4).map { characters[$0..<($0 + 4)].joined() }.joined(separator: "-")
    }

    /// 抄的时候大小写、横线、把 O 写成 0 都算同一个码
    static func normalize(_ code: String) -> String? {
        let map: [Character: Character] = ["O": "0", "I": "1", "L": "1"]
        let clean = code.uppercased().filter { !" -\n\t".contains($0) }.map { map[$0] ?? $0 }
        return clean.count == 24 && clean.allSatisfy({ alphabet.contains($0) }) ? String(clean) : nil
    }

    static func recoveryKey(_ code: String) throws -> P256.Signing.PrivateKey {
        guard let clean = normalize(code) else { throw ChipError.badCode }
        let seed = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: Data(clean.utf8)), salt: Data("lmc-recovery-v1".utf8),
                                          info: Data("p256".utf8), outputByteCount: 32)
        return try seed.withUnsafeBytes { try P256.Signing.PrivateKey(rawRepresentation: Data($0)) }
    }

    static func recoveryPublic(_ code: String) throws -> String { publicText(x963: try recoveryKey(code).publicKey.x963Representation) }

    static func signWithRecovery(_ text: String, code: String) throws -> String {
        let key = try recoveryKey(code)
        return try sshSignature(text, x963: key.publicKey.x963Representation) { try key.signature(for: $0) }
    }
}

// MARK: 签之前先看一遍。系统弹框不显示被签的内容，不看就是盲签：管理版连着的主机被攻破时，
// 它可以拿"登记坏人的钥匙"冒充"关机"，这里挡得住，因为笔记本这边没被攻破

/// 用户点的是哪个最高操作
enum TopIntent {
    case poweroff(String?), handover(String), takeover(String?), candidates([String]), enable([String]), password, addMachine, removeMachine(String)
}

/// 核对时要用到的现状
struct TopContext {
    let enabled: Bool, term: Int, hub: String?, candidates: [String], local: String?
}

extension Chip {
    private static func object(_ text: String) -> [String: Any]? { (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] }

    /// 后端给的要签的内容，是不是用户点的那件事
    static func matches(_ text: String, _ intent: TopIntent, _ context: TopContext) -> Bool {
        guard let body = object(text) else { return false }
        if case .poweroff(let target) = intent {
            guard Set(body.keys) == ["op", "targets", "at", "nonce"], body["op"] as? String == "poweroff", let targets = body["targets"] as? [String], !targets.isEmpty,
                  body["at"] is Int, body["nonce"] is String else { return false }   // 新不新归被关的那台核对：这边和主机的时钟可能差着，不要误报
            return target.map { targets == [$0] } ?? true
        }
        if case .password = intent, !context.enabled { return Set(body.keys) == ["op", "machine", "secret"] && body["op"] as? String == "password" }
        guard Set(body.keys) == ["op", "cluster", "term", "hub", "candidates", "secret", "machines"], body["op"] as? String == "state",
              let term = body["term"] as? Int, let hub = body["hub"] as? String, let listed = body["candidates"] as? [String] else { return false }
        let next = context.term + 1, same = listed == context.candidates.sorted()
        switch intent {
        case .handover(let target): return term == next && hub == target && same
        case .takeover(let target):
            // 管理版手填的名字可能和对方自己认的不同：不比名字，只要新主机在名单里、不是现在这台
            return term == next && same && (target.map { hub == $0 } ?? (listed.contains(hub) && hub != context.hub))
        case .candidates(let names): return term == next && hub == context.hub && listed == Set(names + [hub]).sorted()
        case .enable(let names): return term == 1 && hub == context.local && listed == Set(names + [hub]).sorted()
        case .password, .addMachine: return term == next && hub == context.hub && same
        case .removeMachine(let name): return term == next && hub == context.hub && listed == context.candidates.filter { $0 != name }.sorted()
        case .poweroff: return false
        }
    }

    /// 登记表的更新：钥匙只能多这一把、少这一把，恢复码和开关不能顺手改。recovery 为空表示恢复码不该变
    static func bodyMatches(_ text: String, seq: Int, keys: [String], recovery: String?, recoveryId: String?, passwordOnly: Bool) -> Bool {
        guard let body = object(text), let rows = body["keys"] as? [[String: Any]], body["seq"] as? Int == seq + 1,
              let code = body["recovery"] as? String, let only = body["password_only"] as? Bool else { return false }
        guard rows.compactMap({ ($0["pub"] as? String).map(keyId) }) == keys else { return false }
        guard recovery.map({ $0 == code }) ?? (keyId(code) == recoveryId) else { return false }
        return only == passwordOnly
    }
}

extension Chip {
    /// 机器的内部名字，由完整地址生成：只取最后一段的话，两台 .local 或不同网段的 .20 会撞名，后一台盖掉前一台。
    /// 后端最多 32 个字符（hub.py 的 NODE_NAME、rpc.py 的白名单、clusterctl.py 的 machine_add）：超了就截短，
    /// 后面接完整地址的短哈希，不同地址还是不同名字。没超的照旧，已有的名字不变
    static func machineName(address: String) -> String {
        let lower = address.lowercased()
        var text = lower
        if text.hasSuffix(".local") { text.removeLast(6) }
        let name = "mac-" + text.map { $0.isASCII && ($0.isLetter || $0.isNumber) ? String($0) : "-" }.joined()
        guard name.count > machineNameMax else { return name }
        let hash = SHA256.hash(data: Data(lower.utf8)).map { String(format: "%02x", $0) }.joined().prefix(6)
        var head = String(name.prefix(machineNameMax - 7))
        while head.hasSuffix("-") { head.removeLast() }
        return head + "-" + hash
    }

    static let machineNameMax = 32

    /// 配对核对码：和 clusterctl.py 的 pairing_code 同一算法，两边屏幕上才一样
    static func pairCode(pub: String, hostKey: String) -> String {
        let text = pub.trimmingCharacters(in: .whitespacesAndNewlines) + "\n" + hostKey.trimmingCharacters(in: .whitespacesAndNewlines)
        let hex = SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
        let digits = String(format: "%012llu", (UInt64(hex.prefix(16), radix: 16) ?? 0) % 1_000_000_000_000)
        return digits.prefix(4) + "-" + digits.dropFirst(4).prefix(4) + "-" + digits.dropFirst(8)
    }
}
