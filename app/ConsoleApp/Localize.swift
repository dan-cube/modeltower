import Foundation

/// 界面的中英文。中文当键，英文在 en.lproj/Localizable.strings，查不到显示中文，不会崩。
/// L("关机 \(name)？") 的键是 "关机 %@？"，插值一律是 %@，字面的 % 写成 %%。英文里可以用 %1$@ 调语序。
/// 不用 .environment(\.locale)：同一屏会混两种语言。切语言写 AppleLanguages 再重开，见 Console.setLanguage
struct LocalizedText: ExpressibleByStringInterpolation {
    var key = ""
    var args: [String] = []

    init(stringLiteral value: String) { key = value.replacingOccurrences(of: "%", with: "%%") }

    init(stringInterpolation: Interpolation) {
        key = stringInterpolation.key
        args = stringInterpolation.args
    }

    struct Interpolation: StringInterpolationProtocol {
        var key = ""
        var args: [String] = []

        init(literalCapacity: Int, interpolationCount: Int) {}
        mutating func appendLiteral(_ literal: String) { key += literal.replacingOccurrences(of: "%", with: "%%") }
        mutating func appendInterpolation<T>(_ value: T) {
            key += "%@"
            args.append("\(value)")
        }
    }
}

/// 测试用：环境变量 LMC_I18N_MISSES 指定文件时，记下查不到的键
private let missFile = ProcessInfo.processInfo.environment["LMC_I18N_MISSES"]

func L(_ text: LocalizedText) -> String {
    let marker = "\u{1}"
    var format = Bundle.main.localizedString(forKey: text.key, value: marker, table: nil)
    if format == marker {
        format = text.key
        if let path = missFile, let line = (text.key + "\n").data(using: .utf8), let handle = FileHandle(forWritingAtPath: path) ?? { FileManager.default.createFile(atPath: path, contents: nil); return FileHandle(forWritingAtPath: path) }() {
            handle.seekToEndOfFile()
            handle.write(line)
            try? handle.close()
        }
    }
    return fill(format, text.args)
}

/// 把 %@、%1$@ 换成参数，%% 换成 %。别的 % 写法原样留着
private func fill(_ format: String, _ args: [String]) -> String {
    guard format.contains("%") else { return format }
    var out = "", next = 0
    let chars = Array(format)
    var i = 0
    while i < chars.count {
        guard chars[i] == "%", i + 1 < chars.count else { out.append(chars[i]); i += 1; continue }
        if chars[i + 1] == "%" { out.append("%"); i += 2; continue }
        if chars[i + 1] == "@" {
            if next < args.count { out += args[next] }
            next += 1
            i += 2
            continue
        }
        var j = i + 1, number = 0
        while j < chars.count, let digit = chars[j].wholeNumberValue { number = number * 10 + digit; j += 1 }
        if j + 1 < chars.count, j > i + 1, chars[j] == "$", chars[j + 1] == "@" {
            if number >= 1, number <= args.count { out += args[number - 1] }
            i = j + 2
            continue
        }
        out.append(chars[i])
        i += 1
    }
    return out
}
