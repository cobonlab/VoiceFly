import AppKit
import SwiftUI
import VoiceFlyCore

@main
struct VoiceFlySettingsApp: App {
  @NSApplicationDelegateAdaptor(SettingsApplicationDelegate.self) private var appDelegate
  init() {
    if let index = CommandLine.arguments.firstIndex(of: "--prepare-test-user"),
       CommandLine.arguments.count > index + 1 {
      do {
        let paths = VoiceFlyPaths(home: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        let manager = VoiceFlyWordManager(paths: paths, resources: Bundle.main.resourceURL)
        try manager.initializeLocalConfiguration()
        try manager.put(text: "人工智能", pinyin: "ren gong zhi neng")
        exit(0)
      } catch { exit(1) }
    }
    if CommandLine.arguments.contains("--initialize") {
      do {
        try VoiceFlyWordManager(resources: Bundle.main.resourceURL).initializeLocalConfiguration()
        exit(0)
      } catch {
        fputs("Voice Fly configuration was not initialized: \(error.localizedDescription)\n", stderr)
        exit(1)
      }
    }
  }
  @StateObject private var model = SettingsModel()
  var body: some Scene {
    WindowGroup("Voice Fly 设置") {
      SettingsWindow().environmentObject(model).frame(minWidth: 760, minHeight: 560)
        .onAppear { NSApp.setActivationPolicy(.accessory); NSApp.activate(ignoringOtherApps: true) }
    }
    .windowStyle(.titleBar)
    .commands { CommandGroup(replacing: .newItem) {} }
  }
}

final class SettingsApplicationDelegate: NSObject, NSApplicationDelegate {
  func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@MainActor
final class SettingsModel: ObservableObject {
  @Published var settings = VoiceFlySettings()
  @Published var words: [VoiceFlyWord] = []
  @Published var message = ""
  @Published var busy = false
  @Published var serviceStatus = "正在检查语音服务…"
  @Published var wordText = ""
  @Published var wordPinyin = ""
  @Published var search = ""
  @Published var selectedWord: String?
  let store = VoiceFlySettingsStore()
  let manager = VoiceFlyWordManager(resources: Bundle.main.resourceURL)
  let inputMethod = URL(fileURLWithPath: "/Library/Input Methods/VoiceFly.app/Contents/MacOS/VoiceFlyInput")
  private var timer: Timer?
  init() {
    do {
      try manager.migrateLegacyInstallationIfNeeded()
      settings = try store.load()
      try manager.migrateLegacyIfNeeded()
      words = try manager.list()
    } catch { message = "读取失败：" + error.localizedDescription }
    refreshStatus()
    timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
      Task { @MainActor in self?.refreshStatus() }
    }
  }
  var filteredWords: [VoiceFlyWord] { words.filter { search.isEmpty || $0.text.localizedCaseInsensitiveContains(search) || $0.pinyin.contains(search) } }
  func refreshStatus() {
    let url = store.paths.support.appendingPathComponent("service-status.json")
    if let data = try? Data(contentsOf: url),
       let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
       let updated = object["updated"] as? Double, Date().timeIntervalSince1970 - updated < 30 {
      let phase = object["phase"] as? String ?? "idle"
      switch phase {
      case "idle": serviceStatus = "语音服务已就绪 · 识别在本机完成"
      case "loading": serviceStatus = "语音模型正在加载，请稍候…"
      case "error": serviceStatus = "语音服务启动失败，请重新安装完整组件，再点重启语音服务。"
      default: serviceStatus = "语音服务正在工作"
      }
    } else { serviceStatus = "语音服务未就绪，请点“重启语音服务”后再检查。" }
  }
  func save() {
    do {
      try store.prepareRime(settings)
      try manager.regenerate()
      try store.save(settings)
      message = "设置已保存。当前输入结束后生效；正在说话时不会打断。"
    } catch { message = "保存失败：" + error.localizedDescription }
  }
  func saveWord() {
    do {
      if wordPinyin.isEmpty { wordPinyin = manager.pronunciation(wordText) }
      try manager.put(text: wordText, pinyin: wordPinyin)
      words = try manager.list()
      message = "词语已保存，语音和文字输入共用这份词库。"
      wordText = ""; wordPinyin = ""; selectedWord = nil
      requestDeploy()
    } catch { message = "词语未保存：" + error.localizedDescription }
  }
  func deleteWord() {
    guard let selectedWord else { return }
    do {
      try manager.remove(text: selectedWord)
      words = try manager.list()
      message = "已从个人词库删除。基础词典和其他独立词典不会删除。"
      self.selectedWord = nil; wordText = ""; wordPinyin = ""
      requestDeploy()
    } catch { message = "删除失败：" + error.localizedDescription }
  }
  func selectWord(_ id: String?) {
    guard let word = words.first(where: { $0.id == id }) else { return }
    wordText = word.text; wordPinyin = word.pinyin
  }
  func requestDeploy() {
    DistributedNotificationCenter.default().postNotificationName(
      Notification.Name("com.simonli.voicefly.settings-changed"), object: nil, userInfo: nil, deliverImmediately: true)
  }
  func run(_ executable: URL, _ arguments: [String], success: String) {
    guard !busy else { return }
    busy = true; message = "正在处理…"
    DispatchQueue.global(qos: .userInitiated).async { [weak self] in
      let process = Process(); process.executableURL = executable; process.arguments = arguments
      // Do not collect recognition text or a private dictionary into UI logs.
      process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
      var status: Int32 = -1
      do { try process.run(); process.waitUntilExit(); status = process.terminationStatus } catch {}
      let succeeded = status == 0
      Task { @MainActor in
        self?.busy = false
        self?.message = succeeded ? success : "处理失败，请查看帮助或重新安装。原数据仍保留。"
        self?.refreshStatus()
      }
    }
  }
  func activate() {
    do {
      try store.prepareRime(settings); try manager.regenerate(); try store.save(settings)
      run(inputMethod, ["--build"], success: "输入方案已准备好。请点“启用输入法”，再从右上角系统菜单选择 Voice Fly。")
    } catch { message = error.localizedDescription }
  }
  func enable() {
    // Out-of-process selection can leave IMK inactive on macOS 26 even when
    // TIS reports success. Let the user select through the native input menu.
    run(inputMethod, ["--enable-input-source"], success: "已请求启用。请从右上角系统输入法菜单选择 Voice Fly；若刚更新过输入法，请保存工作并注销 Mac 后重新登录。")

  }
  func restartService() {
    run(URL(fileURLWithPath: "/bin/launchctl"), ["kickstart", "-k", "gui/\(getuid())/com.simonli.voicefly.service"], success: "已请求重启语音服务，稍候状态会更新。")
  }
  func importWords() {
    let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]; panel.canChooseDirectories = false
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do { try manager.importWords(from: url); words = try manager.list(); requestDeploy(); message = "已合并导入，原有词语保留。" }
    catch { message = "导入失败：" + error.localizedDescription }
  }
  func exportWords() {
    let panel = NSSavePanel(); panel.nameFieldStringValue = "VoiceFly-personal-words.json"; panel.allowedContentTypes = [.json]
    guard panel.runModal() == .OK, let url = panel.url else { return }
    do { try manager.export(to: url); message = "个人词库已导出，请保存在自己信任的位置。" }
    catch { message = "导出失败：" + error.localizedDescription }
  }
  func open(_ value: String) { if let url = URL(string: value) { NSWorkspace.shared.open(url) } }
}

struct SettingsWindow: View {
  @EnvironmentObject private var model: SettingsModel
  @State private var tab = 0
  @State private var confirmDelete = false
  var body: some View {
    VStack(alignment: .leading, spacing: 16) {
      HStack {
        if let url = Bundle.main.url(forResource: "VoiceFlyMark", withExtension: "png"),
           let image = NSImage(contentsOf: url) {
          Image(nsImage: image).resizable().frame(width: 44, height: 44)
        } else {
          Image(systemName: "waveform.circle.fill").font(.system(size: 40)).foregroundStyle(.blue)
        }
        VStack(alignment: .leading) {
          Text("Voice Fly").font(.title.bold())
          Text("让想法更快变成文字").foregroundStyle(.secondary)
        }
        Spacer()
        Text("0.2.0 预览版").font(.caption).foregroundStyle(.secondary)
      }
      TabView(selection: $tab) {
        setup.tabItem { Label("开始使用", systemImage: "checkmark.circle") }.tag(0)
        keyboard.tabItem { Label("文字输入", systemImage: "keyboard") }.tag(1)
        voice.tabItem { Label("语音与按键", systemImage: "mic") }.tag(2)
        words.tabItem { Label("个人词库", systemImage: "text.book.closed") }.tag(3)
        help.tabItem { Label("状态与帮助", systemImage: "questionmark.circle") }.tag(4)
      }
      if !model.message.isEmpty { Text(model.message).font(.callout).textSelection(.enabled).padding(10).frame(maxWidth: .infinity, alignment: .leading).background(.quaternary, in: RoundedRectangle(cornerRadius: 8)) }
      HStack {
        Text("个人词库默认保存在这台电脑，不需要 iCloud。").font(.caption).foregroundStyle(.secondary)
        Spacer()
        if model.busy { ProgressView().controlSize(.small) }
        Button("保存设置", action: model.save).buttonStyle(.borderedProminent).disabled(model.busy)
      }
    }.padding(22)
  }
  private var setup: some View {
    VStack(alignment: .leading, spacing: 18) {
      Text("第一次使用，按这三步完成").font(.title2.bold())
      Text("1. 选择你熟悉的中文输入方式。全拼、双拼、五笔都可以使用语音输入。")
      Picker("输入方式", selection: $model.settings.schema) {
        ForEach(VoiceFlySchema.allCases) { Text($0.title).tag($0) }
      }.frame(maxWidth: 420)
      HStack { Button("准备输入方案", action: model.activate); Button("启用输入法", action: model.enable) }.disabled(model.busy)
      Text("启用后，请从右上角系统输入法菜单选择 Voice Fly。更新输入法本体后，需要保存工作并注销 Mac，再重新登录。")
      Text("2. 在系统设置中允许 Voice Fly Service 使用麦克风。")
      Button("打开麦克风权限") { model.open("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") }
      Text("3. 打开文稿，先打一段中文，再按住语音键说话，松开后看结果。")
      Text("双击空格后按 2–5，保存光标前相应字数的词语。多音字可以在“个人词库”里检查和修改读音。")
        .foregroundStyle(.secondary)
      Text(model.serviceStatus).font(.callout)
      Toggle("我已完成文字与语音试用", isOn: $model.settings.completedSetup)
      Spacer()
    }.padding(18)
  }
  private var keyboard: some View {
    Form {
      Picker("中文输入方式", selection: $model.settings.schema) { ForEach(VoiceFlySchema.allCases) { Text($0.title).tag($0) } }
      Picker("每页候选词数量", selection: $model.settings.candidateCount) { Text("5 个").tag(5); Text("9 个").tag(9); Text("25 个（5×5）").tag(25) }
      Toggle("使用简体中文", isOn: $model.settings.simplified)
      Toggle("使用全角字符", isOn: $model.settings.fullShape)
      Toggle("键盘符号直接按原键输出", isOn: $model.settings.directSymbols)
      Toggle("记录个人选词习惯", isOn: $model.settings.personalLearning)
      Text("方案切换后，你已经明确保存的词语会继续参与输入。五笔使用 86 版码表；暂不提供 98 版、新世纪版和其他全部输入法。")
        .font(.callout).foregroundStyle(.secondary)
    }.padding(18)
  }
  private var voice: some View {
    Form {
      Toggle("启用按住说话", isOn: $model.settings.voiceEnabled)
      Picker("按住说话的实体按键", selection: $model.settings.voiceKey) { Text("Control").tag("control"); Text("Option").tag("option") }
      Text("按住后开始，松开后结束。组合快捷键和录音中打字会取消本次录音。Option 可能与你已有的快捷键冲突，可随时改回 Control。")
        .font(.callout).foregroundStyle(.secondary)
      Toggle("双击空格后用数字保存词语", isOn: $model.settings.spaceLearning)
      Text("关闭后，空格立即按普通输入处理。三击空格的拆字功能也同时关闭。")
        .font(.callout).foregroundStyle(.secondary)
      Toggle("优先使用电脑内置麦克风", isOn: $model.settings.preferBuiltinMicrophone)
      Text("关闭时，每次录音跟随系统当前默认麦克风。蓝牙耳机切换后无需重启输入法。")
        .font(.callout).foregroundStyle(.secondary)
      Text("识别在本机完成。本版本不调用 OpenAI API，不上传录音，也不保存录音文件。")
        .font(.callout).foregroundStyle(.secondary)
    }.padding(18)
  }
  private var words: some View {
    VStack(alignment: .leading, spacing: 10) {
      TextField("搜索词语或拼音", text: $model.search)
      HStack {
        List(selection: $model.selectedWord) {
          ForEach(model.filteredWords) { word in
            HStack { Text(word.text); Spacer(); Text(word.pinyin).foregroundStyle(.secondary) }.tag(word.id)
          }
        }.onChange(of: model.selectedWord) { model.selectWord($0) }
        VStack(alignment: .leading, spacing: 12) {
          Text("添加或修改读音").font(.headline)
          TextField("词语（2–12 个汉字）", text: $model.wordText)
          TextField("读音，如 ren gong zhi neng", text: $model.wordPinyin)
          Button("填写建议读音") { model.wordPinyin = model.manager.pronunciation(model.wordText) }
          Button("保存词语", action: model.saveWord).buttonStyle(.borderedProminent)
          Button("删除选中词语", role: .destructive) { confirmDelete = true }.disabled(model.selectedWord == nil)
          Text("保存会覆盖同名词的读音。改词名时，请先保存新词，再删除旧词。")
            .font(.caption).foregroundStyle(.secondary)
          Spacer()
        }.frame(width: 245)
      }
      HStack { Text("共 \(model.words.count) 个词语").foregroundStyle(.secondary); Spacer(); Button("导入", action: model.importWords); Button("导出", action: model.exportWords) }
    }.padding(18).confirmationDialog("从个人词库删除这个词语？", isPresented: $confirmDelete) {
      Button("删除", role: .destructive, action: model.deleteWord)
    }
  }
  private var help: some View {
    VStack(alignment: .leading, spacing: 18) {
      Text(model.serviceStatus).font(.headline)
      HStack { Button("检查状态", action: model.refreshStatus); Button("重启语音服务", action: model.restartService).disabled(model.busy) }
      Text("没有声音或识别失败：检查麦克风权限和系统默认输入设备，再重启语音服务。没有候选词：回到“开始使用”准备输入方案。")
      HStack {
        Button("打开个人数据文件夹") { NSWorkspace.shared.open(model.store.paths.support) }
        Button("打开高级输入配置") { NSWorkspace.shared.open(model.store.paths.rime) }
      }
      Text("修改个人词库前会自动留一份备份，保存在个人数据文件夹的 Backups 中。导出会包含你的个人词语，请勿公开上传。")
        .font(.callout).foregroundStyle(.secondary)
      HStack {
        Button("查看新版与更新说明") { model.open("https://github.com/cobonlab/VoiceFly/releases") }
        Button("反馈问题") { model.open("https://github.com/cobonlab/VoiceFly/issues") }
      }
      Text("测试版仍未经过 Apple 签名和公证。当前仅在 Apple 芯片 Mac 上验证。卸载与恢复步骤请查看仓库说明。")
        .font(.callout).foregroundStyle(.secondary)
      Spacer()
    }.padding(18)
  }
}
