//
//  SquirrelInputController.swift
//  Squirrel
//
//  Created by Leo Liu on 5/7/24.
//

import InputMethodKit

final class SquirrelInputController: IMKInputController {
  private static let keyRollOver = 50
  private static let chaiziMarker = "~"
  private static var unknownAppCnt: UInt = 0
  private static var personalUsageURL: URL { VoiceFlyPaths().words }
  private static let personalUsageStore = VoiceFlyPersonalUsageStore(url: personalUsageURL)
  private static var wordManager: VoiceFlyWordManager {
    VoiceFlyWordManager(resources: URL(fileURLWithPath: Bundle.main.sharedSupportPath!))
  }
  private var productSettings = (try? VoiceFlySettingsStore().load()) ?? VoiceFlySettings()
  private var voiceModifier: NSEvent.ModifierFlags { productSettings.voiceKey == "option" ? .option : .control }
  static func requestSettingsReload() {
    learnedPhraseDeployPending = true
    if let activeController { activeController.deployLearnedPhrasesIfSafe() }
    else {
      DispatchQueue.main.async {
        guard activeController == nil, learnedPhraseDeployPending else { return }
        learnedPhraseDeployPending = false
        NSApp.squirrelAppDelegate.deploy()
      }
    }
  }
  private static let learnedPhraseStore = VoiceFlyLearnedPhraseStore(
    url: SquirrelApp.userDir.appendingPathComponent("voicefly_learned_phrases.txt")
  )
  private static let learnedPhraseWriteQueue = DispatchQueue(
    label: "com.simonli.voicefly.learned-phrases",
    qos: .utility
  )
  private static let learnedPhraseWriteGroup = DispatchGroup()
  private static var learnedPhraseDeployPending = false
  private static var learnedPhraseDeployScheduled = false
  private static weak var activeController: SquirrelInputController?

  static func hasConnectedInputSession() -> Bool {
    guard SquirrelInstaller.currentInputSourceID() == SquirrelInstaller.InputMode.primary.rawValue,
          let controller = activeController, controller.client != nil,
          controller.session != 0 else { return false }
    return controller.rimeAPI.find_session(controller.session)
  }

  private static func recordPersonalUsage(text: String, code: String, explicit: Bool) {
    personalUsageStore.record(text: text, code: code, explicit: explicit)
  }

  @discardableResult
  static func flushPersonalUsage(timeout: TimeInterval = 0.5) -> Bool {
    let startedAt = ProcessInfo.processInfo.systemUptime
    let learnedPhrasesFlushed = learnedPhraseWriteGroup.wait(
      timeout: .now() + max(0, timeout)
    ) == .success
    let elapsed = ProcessInfo.processInfo.systemUptime - startedAt
    let personalUsageFlushed = personalUsageStore.flushSynchronously(
      timeout: max(0, timeout - elapsed)
    )
    return learnedPhrasesFlushed && personalUsageFlushed
  }

  private weak var client: IMKTextInput?
  private let rimeAPI: RimeApi_stdbool = rime_get_api_stdbool().pointee
  private var preedit: String = ""
  private var selRange: NSRange = .empty
  private var caretPos: Int = 0
  private var lastModifiers: NSEvent.ModifierFlags = .init()
  private var shiftUsedForDirectSymbol = false
  private var asciiModeBeforeDirectSymbolShift = false
  private var session: RimeSessionId = 0
  private var schemaId: String = ""
  private var inlinePreedit = false
  private var inlineCandidate = false
  private var chordKeyCodes: [UInt32] = .init(repeating: 0, count: SquirrelInputController.keyRollOver)
  private var chordModifiers: [UInt32] = .init(repeating: 0, count: SquirrelInputController.keyRollOver)
  private var chordKeyCount: Int = 0
  private var chordTimer: Timer?
  private var chordDuration: TimeInterval = 0
  private var currentApp: String = ""
  private var voiceArmTimer: Timer?
  private var voiceHoldMonitorTimer: Timer?
  private var voiceStartWatchdog: Timer?
  private var voiceFinalWatchdog: Timer?
  private var voiceArmed = false
  private var voiceActive = false
  private var voiceStopRequested = false
  private var voiceSessionID = ""
  private var voicePartialState = VoiceFlyVoicePartialState()
  private var voiceHoldReleaseDetector = VoiceFlyVoiceHoldReleaseDetector()
  private var phraseLearningJournal = VoiceFlyPhraseLearningJournal()
  private var phraseCommitInputHint: String?
  private var phraseSpaceGesture = VoiceFlySpaceGesture()
  private var recentPhraseCommit = VoiceFlyRecentCommit()
  private var pendingPhraseLearningSpaceTimer: Timer?
  private var phraseLengthSelectionTimer: Timer?
  private var phraseLengthChoices: [Int: VoiceFlyPhraseDraft] = [:]
  private var chaiziMode = false
  private var chaiziComponents: [String] = []
  private var chaiziResults: [String] = []
  private var chaiziHighlightedIndex = 0
  private var chaiziQuerySpaceCount = 0
  private var chaiziQuerySpaceStartedAt: Date?

  private static let chaiziIndex = loadChaiziIndex()
  private static let pinyinLexicon = loadPinyinLexicon()

  static func preloadLearningResources() {
    DispatchQueue.global(qos: .utility).async {
      // Swift's lazy static initialization is thread-safe.  Starting both
      // indexes at process launch removes their first-use pause from typing.
      _ = pinyinLexicon
      _ = chaiziIndex
      _ = personalUsageStore.refreshMirrorSynchronously(timeout: 5)
    }
  }

  private static let voiceStartNotification = Notification.Name("com.simonli.voicefly.start")
  private static let voiceStopNotification = Notification.Name("com.simonli.voicefly.stop")
  private static let voiceStartedNotification = Notification.Name("com.simonli.voicefly.started")
  private static let voiceMicReadyNotification = Notification.Name("com.simonli.voicefly.mic-ready")
  private static let voicePartialNotification = Notification.Name("com.simonli.voicefly.partial")
  private static let voiceFinalNotification = Notification.Name("com.simonli.voicefly.final")
  private static let voiceErrorNotification = Notification.Name("com.simonli.voicefly.error")

  // swiftlint:disable:next cyclomatic_complexity
  override func handle(_ event: NSEvent!, client sender: Any!) -> Bool {
    guard let event = event else { return false }
    if IsSecureEventInputEnabled() {
      cancelVoiceArm()
      if voiceActive { cancelVoiceInput(reason: "secure input") }
      cancelPhraseLearningState()
      return false
    }
    let modifiers = event.modifierFlags
    let changes = lastModifiers.symmetricDifference(modifiers)

    // Return true to consume the key event; return false to pass it to the client app.
    var handled = false

    self.client ?= sender as? IMKTextInput
    if event.type == .keyDown, chaiziMode {
      // Manual-mode teardown must not depend on Rime session health. Session
      // recreation exits chaizi, so checking only later in the keyDown branch
      // would let this same Backspace or `~` continue into the host document.
      if Self.isPhysicalBackspace(event) {
        if session != 0, rimeAPI.find_session(session) {
          handleChaiziBackspace()
        } else {
          exitChaiziMode()
          SquirrelApplicationDelegate.showMessage(msgText: "拆字会话已失效，已安全退出")
        }
        deployLearnedPhrasesIfSafe()
        return true
      }
      let isEscape = event.keyCode == 53 || event.charactersIgnoringModifiers?.first == "\u{1b}"
      if isEscape || VoiceFlyChaiziInteractionRules.isTildeExit(event.characters?.first) {
        exitChaiziMode()
        SquirrelApplicationDelegate.showMessage(msgText: "已退出拆字模式")
        deployLearnedPhrasesIfSafe()
        return true
      }
    }

    if session == 0 || !rimeAPI.find_session(session) {
      createSession()
      if session == 0 {
        return false
      }
    }

    Self.activeController = self
    if let app = client?.bundleIdentifier(), currentApp != app {
      if chaiziMode { exitChaiziMode() }
      cancelPhraseLearningState()
      currentApp = app
      updateAppOptions()
    }

    switch event.type {
    case .flagsChanged:
      // A modifier after a pending space must not let the delayed space land
      // after the shortcut or in another application.
      settlePendingPhraseLearningGesture()
      cancelPhraseLengthSelection(clearLearningContext: true)
      recentPhraseCommit.invalidate()
      if lastModifiers == modifiers {
        handled = true
        break
      }

      // A bare Control hold is the voice key. Delay activation briefly so
      // Control shortcuts (Ctrl-C, Ctrl-[, etc.) keep their normal behavior.
      let shortcutModifiers = modifiers.intersection([.control, .shift, .option, .command])
      let isBareControl = productSettings.voiceEnabled && shortcutModifiers == voiceModifier
      let wasVoiceArmed = voiceArmed
      let triggerKeys: Set<UInt16> = productSettings.voiceKey == "option" ? [58, 61] : [59, 62]
      let isExplicitControlRelease = triggerKeys.contains(event.keyCode) &&
        changes.contains(voiceModifier) && !modifiers.contains(voiceModifier)

      // Adding another shortcut modifier must disarm voice even when Control
      // itself did not change in this flagsChanged event.
      if voiceArmed && !isBareControl {
        cancelVoiceArm()
      }
      if voiceActive && !isBareControl {
        if modifiers.contains(voiceModifier) {
          cancelVoiceInput(reason: "shortcut modifier added")
        } else if isExplicitControlRelease {
          stopVoiceInput(reason: "physical-control-key-up")
          lastModifiers = modifiers
          handled = true
          break
        } else {
          // AirPods microphone mode can briefly make a Bluetooth keyboard's
          // combined modifier state look empty.  An unrelated flagsChanged
          // event is not proof that Control was released; the hold monitor
          // below confirms a sustained release instead.
          voiceLog("ignored non-Control flagsChanged while Control state was absent keyCode=\(event.keyCode)")
        }
      }

      if changes.contains(voiceModifier) {
        if isBareControl {
          armVoiceInput(sender)
          lastModifiers = modifiers
          handled = true
          break
        }
        if !modifiers.contains(voiceModifier) {
          if wasVoiceArmed {
            lastModifiers = modifiers
            handled = true
            break
          }
        }
      }
      var rimeModifiers: UInt32 = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
      // Some remote desktop tools send flagsChanged with keyCode 0; infer the real modifier key when needed.
      var keyCode = event.keyCode
      if !SquirrelKeycode.modifierKeycodes.contains(keyCode) {
        guard let inferred = SquirrelKeycode.inferModifierKeycode(from: changes) else {
          lastModifiers = modifiers
          rimeUpdate()
          handled = true
          break
        }
        keyCode = inferred
      }
      let rimeKeycode: UInt32 = SquirrelKeycode.osxKeycodeToRime(keycode: keyCode, keychar: nil, shift: false, caps: false)

      if changes.contains(.capsLock) {
        // Rime expects XK_Caps_Lock before the lock mask changes; NSFlagsChanged has already applied it.
        rimeModifiers ^= kLockMask.rawValue
        _ = processKey(rimeKeycode, modifiers: rimeModifiers)
      }

      // Process releases first because some modifier releases arrive with the next keydown.
      var buffer = [(keycode: UInt32, modifier: UInt32)]()
      for flag in [NSEvent.ModifierFlags.shift, .control, .option, .command] where changes.contains(flag) {
        if modifiers.contains(flag) {
          buffer.append((keycode: rimeKeycode, modifier: rimeModifiers))
        } else {
          buffer.insert((keycode: rimeKeycode, modifier: rimeModifiers | kReleaseMask.rawValue), at: 0)
        }
      }
      for (keycode, modifier) in buffer {
        _ = processKey(keycode, modifiers: modifier)
      }

      // Rime normally treats Shift release as the Chinese/English toggle.
      // When Shift was held to type a direct symbol such as ?, @ or %, that
      // release belongs to the key combination and must not change modes.
      if shiftUsedForDirectSymbol,
         changes.contains(.shift),
         !modifiers.contains(.shift) {
        if rimeAPI.get_option(session, "ascii_mode") != asciiModeBeforeDirectSymbolShift {
          rimeAPI.set_option(session, "ascii_mode", asciiModeBeforeDirectSymbolShift)
        }
        shiftUsedForDirectSymbol = false
      }

      lastModifiers = modifiers
      rimeUpdate()

    case .keyDown:
      let isPlainSpaceEvent = event.charactersIgnoringModifiers?.first == " " &&
        modifiers.intersection([.command, .control, .option, .shift]).isEmpty
      if isPlainSpaceEvent && event.isARepeat {
        settlePendingPhraseLearningGesture(forKeyRepeat: true)
      } else if !isPlainSpaceEvent {
        // Settle a pending single/double tap before this key so delayed spaces
        // can never be inserted after newer text.
        settlePendingPhraseLearningGesture()
      }
      if handlePhraseLengthSelectionKeyDown(event) {
        handled = true
        break
      }
      if !isPlainSpaceEvent {
        recentPhraseCommit.invalidate()
      }
      if voiceArmed {
        cancelVoiceArm()
      }
      if voiceActive {
        // A key pressed after the 0.1 s threshold is still a shortcut/typing
        // intent. Discard the voice composition and let the key continue.
        cancelVoiceInput(reason: "key pressed while recording")
      }
      // Let client apps handle Command shortcuts.
      if modifiers.contains(.command) {
        if chaiziMode { exitChaiziMode() }
        phraseLearningJournal.reset()
        break
      }

      if handleChaiziKeyDown(event) {
        handled = true
        break
      }

      // With no composition, cursor movement belongs entirely to the host.
      // Do not refresh marked text or query its caret rectangle on every
      // arrow/repeat: some remote text clients change their editing state in
      // response, even though Rime declined the key.
      let hasComposition = rimeAPI.get_input(session).map {
        !String(cString: $0).isEmpty
      } ?? false
      if !hasComposition && !chaiziMode &&
          ([UInt16(123), 124].contains(event.keyCode) ||
           (isPlainSpaceEvent && event.isARepeat)) {
        phraseLearningJournal.reset()
        recentPhraseCommit.invalidate()
        break
      }

      // Printable symbols already present on the physical keyboard must never
      // enter Rime's punctuator.  In particular, a real Shift+2 event carries
      // both the resolved "@" character and a Shift modifier; letting Rime see
      // that event can still open a punctuation candidate menu depending on
      // the session's saved options.  Commit the current Chinese composition,
      // then insert the resolved ASCII symbol directly through IMK.
      if let symbol = directKeyboardSymbol(from: event) {
        if chaiziMode { exitChaiziMode() }
        if modifiers.contains(.shift), !shiftUsedForDirectSymbol {
          asciiModeBeforeDirectSymbolShift = rimeAPI.get_option(session, "ascii_mode")
          shiftUsedForDirectSymbol = true
        }
        if let rawInput = rimeAPI.get_input(session), !String(cString: rawInput).isEmpty {
          phraseCommitInputHint = String(cString: rawInput)
          if rimeAPI.commit_composition(session) {
            rimeUpdate()
          } else {
            commitComposition(client)
          }
          phraseCommitInputHint = nil
        }
        phraseLearningJournal.reset()
        commit(string: symbol)
        handled = true
        break
      }

      let keyCode = event.keyCode
      var keyChars = event.charactersIgnoringModifiers
      let capitalModifiers = modifiers.isSubset(of: [.shift, .capsLock])
      if let code = keyChars?.first,
         (capitalModifiers && !code.isLetter) || (!capitalModifiers && !code.isASCII) {
        keyChars = event.characters
      }
      if let char = keyChars?.first {
        let rimeKeycode = SquirrelKeycode.osxKeycodeToRime(keycode: keyCode, keychar: char,
                                                           shift: modifiers.contains(.shift),
                                                           caps: modifiers.contains(.capsLock))
        if rimeKeycode != 0 {
          let rimeModifiers = SquirrelKeycode.osxModifiersToRime(modifiers: modifiers)
          if char == " " && rimeModifiers == 0 && !event.isARepeat && handlePhraseLearningSpace() {
            handled = true
          } else {
            let hadComposition = rimeAPI.get_input(session).map {
              !String(cString: $0).isEmpty
            } ?? false
            phraseCommitInputHint = hadComposition
              ? rimeAPI.get_input(session).map(String.init(cString:))
              : nil
            let isUnmodifiedSpace = char == " " && rimeModifiers == 0
            if isUnmodifiedSpace && hadComposition {
              // Do not let a previous direct/voice commit masquerade as the
              // composition that this physical Space is about to commit.
              recentPhraseCommit.invalidate()
            }
            handled = processKey(rimeKeycode, modifiers: rimeModifiers)
            rimeUpdate()
            phraseCommitInputHint = nil
            updatePhraseLearningJournalAfterKey(
              char: char,
              keycode: rimeKeycode,
              modifiers: rimeModifiers,
              hadComposition: hadComposition,
              handled: handled
            )
            if isUnmodifiedSpace && hadComposition && !event.isARepeat {
              let compositionIsNowEmpty = rimeAPI.get_input(session).map {
                String(cString: $0).isEmpty
              } ?? true
              if handled, compositionIsNowEmpty {
                // The first physical Space already committed the candidate,
                // so it participates in the gesture even when the internal
                // commit journal could not resolve a code.  The timely second
                // tap re-reads the text that actually reached the host and is
                // the authority for learning; this first tap must never be
                // emitted again if no second tap arrives.
                registerPhraseLearningSpace(
                  at: ProcessInfo.processInfo.systemUptime,
                  fallbackSpaces: 0
                )
              }
            } else if isUnmodifiedSpace {
              // ASCII mode and other declined Space paths belong to the host,
              // not to a previous trusted commit transaction.
              recentPhraseCommit.invalidate()
            }
            if chaiziMode && !handled {
              // Navigation, focus traversal, and other keys declined by Rime
              // continue to the host only after the manual mode is torn down.
              exitChaiziMode()
            }
          }
        }
      }

    default:
      break
    }

    deployLearnedPhrasesIfSafe()
    return handled
  }

  func selectCandidate(_ index: Int) -> Bool {
    settlePendingPhraseLearningGesture()
    phraseCommitInputHint = rimeAPI.get_input(session).map(String.init(cString:))
    let success = rimeAPI.select_candidate_on_current_page(session, index)
    if success {
      rimeUpdate()
    }
    phraseCommitInputHint = nil
    deployLearnedPhrasesIfSafe()
    return success
  }

  // swiftlint:disable:next identifier_name
  func page(up: Bool) -> Bool {
    var handled = false
    handled = rimeAPI.change_page(session, up)
    if handled {
      rimeUpdate()
    }
    return handled
  }

  func moveCaret(forward: Bool) -> Bool {
    let currentCaretPos = rimeAPI.get_caret_pos(session)
    guard let input = rimeAPI.get_input(session) else { return false }
    if forward {
      if currentCaretPos <= 0 {
        return false
      }
      rimeAPI.set_caret_pos(session, currentCaretPos - 1)
    } else {
      let inputStr = String(cString: input)
      if currentCaretPos >= inputStr.utf8.count {
        return false
      }
      rimeAPI.set_caret_pos(session, currentCaretPos + 1)
    }
    rimeUpdate()
    return true
  }

  override func recognizedEvents(_ sender: Any!) -> Int {
    return Int(NSEvent.EventTypeMask.Element(arrayLiteral: .keyDown, .flagsChanged).rawValue)
  }

  override func activateServer(_ sender: Any!) {
    self.client ?= sender as? IMKTextInput
    Self.activeController = self
    if chaiziMode { exitChaiziMode() }
    cancelPhraseLearningState()
    var keyboardLayout = NSApp.squirrelAppDelegate.config?.getString("keyboard_layout") ?? ""
    if keyboardLayout == "last" || keyboardLayout == "" {
      keyboardLayout = ""
    } else if keyboardLayout == "default" {
      keyboardLayout = "com.apple.keylayout.ABC"
    } else if !keyboardLayout.hasPrefix("com.apple.keylayout.") {
      keyboardLayout = "com.apple.keylayout.\(keyboardLayout)"
    }
    if keyboardLayout != "" {
      client?.overrideKeyboard(withKeyboardNamed: keyboardLayout)
    }
    // Activation delivers no flagsChanged event, and NSEvent.modifierFlags
    // only reflects this process's own event stream, so lastModifiers may
    // disagree with the actual Caps Lock state by now. Seed it from the
    // session-wide hardware state; otherwise the next Caps Lock press can
    // compare equal to the stale lastModifiers and be dropped by the
    // early-return in handle().
    if CGEventSource.flagsState(.combinedSessionState).contains(.maskAlphaShift) {
      lastModifiers.insert(.capsLock)
    } else {
      lastModifiers.remove(.capsLock)
    }
    preedit = ""
    caretPos = 0
    selRange = NSRange(location: 0, length: 0)
    if session != 0 {
      let state = rimeAPI.get_option(session, "ascii_mode")
      let label = rimeAPI.get_state_label_abbreviated(session, "ascii_mode", state, true).asString
      NSApp.squirrelAppDelegate.updateStatusIcon(asciiMode: state, schemaLabel: label)
    }
    deployLearnedPhrasesIfSafe()
  }

  override init!(server: IMKServer!, delegate: Any!, client: Any!) {
    self.client = client as? IMKTextInput
    super.init(server: server, delegate: delegate, client: client)
    createSession()

    NotificationCenter.default.addObserver(
      forName: .init("SquirrelSetASCIIModeNotification"),
      object: nil,
      queue: nil
    ) { [weak self] notification in
      self?.handleASCIIModeToggle(notification)
    }

    NotificationCenter.default.addObserver(
      forName: .init("SquirrelReportASCIIModeNotification"),
      object: nil,
      queue: nil
    ) { [weak self] notification in
      self?.reportASCIIMode(notification)
    }

    let distributedCenter = DistributedNotificationCenter.default()
    distributedCenter.addObserver(
      self,
      selector: #selector(handleVoiceStarted(_:)),
      name: Self.voiceStartedNotification,
      object: nil
    )
    distributedCenter.addObserver(
      self,
      selector: #selector(handleVoiceMicReady(_:)),
      name: Self.voiceMicReadyNotification,
      object: nil
    )
    distributedCenter.addObserver(
      self,
      selector: #selector(handleVoicePartial(_:)),
      name: Self.voicePartialNotification,
      object: nil
    )
    distributedCenter.addObserver(
      self,
      selector: #selector(handleVoiceFinal(_:)),
      name: Self.voiceFinalNotification,
      object: nil
    )
    distributedCenter.addObserver(
      self,
      selector: #selector(handleVoiceError(_:)),
      name: Self.voiceErrorNotification,
      object: nil
    )
  }

  override func deactivateServer(_ sender: Any!) {
    settlePendingPhraseLearningGesture()
    cancelVoiceArm()
    if voiceActive {
      cancelVoiceInput(reason: "input source deactivated")
    }
    if chaiziMode {
      exitChaiziMode()
    }
    hidePalettes()
    commitComposition(sender)
    cancelPhraseLearningState()
    deployLearnedPhrasesIfSafe()
    if Self.activeController === self {
      Self.activeController = nil
    }
    client = nil
  }

  override func hidePalettes() {
    NSApp.squirrelAppDelegate.panel?.hide()
    super.hidePalettes()
  }

  override func commitComposition(_ sender: Any!) {
    self.client ?= sender as? IMKTextInput
    if session != 0 {
      if let input = rimeAPI.get_input(session) {
        commit(string: String(cString: input))
        rimeAPI.clear_composition(session)
      }
    }
  }

  override func menu() -> NSMenu! {
    let deploy = NSMenuItem(title: NSLocalizedString("Deploy", comment: "Menu item"), action: #selector(deploy), keyEquivalent: "`")
    deploy.target = self
    deploy.keyEquivalentModifierMask = [.control, .option]
    let sync = NSMenuItem(title: NSLocalizedString("Sync user data", comment: "Menu item"), action: #selector(syncUserData), keyEquivalent: "")
    sync.target = self
    let logDir = NSMenuItem(title: NSLocalizedString("Logs...", comment: "Menu item"), action: #selector(openLogFolder), keyEquivalent: "")
    logDir.target = self
    let setting = NSMenuItem(title: NSLocalizedString("Settings...", comment: "Menu item"), action: #selector(openSettings), keyEquivalent: "")
    setting.target = self
    let wiki = NSMenuItem(title: NSLocalizedString("Rime Wiki...", comment: "Menu item"), action: #selector(openWiki), keyEquivalent: "")
    wiki.target = self
    let menu = NSMenu()
    menu.addItem(deploy)
    menu.addItem(sync)
    menu.addItem(logDir)
    menu.addItem(setting)
    menu.addItem(wiki)

    return menu
  }

  @objc func deploy() {
    NSApp.squirrelAppDelegate.deploy()
  }

  @objc func syncUserData() {
    NSApp.squirrelAppDelegate.syncUserData()
  }

  @objc func openLogFolder() {
    NSApp.squirrelAppDelegate.openLogFolder()
  }

  @objc func openSettings() {
    NSApp.squirrelAppDelegate.openSettings()
  }

  @objc func openRimeFolder() {
    NSApp.squirrelAppDelegate.openRimeFolder()
  }

  @objc func openWiki() {
    NSApp.squirrelAppDelegate.openWiki()
  }

  private(set) var specialCommentIndices: [ReservedPropertyKey: Set<Int>] = [:]

  func handleReservedProperty(key rawKey: String, value rawValue: String, for sessionId: RimeSessionId) throws(ReservedPropertyError) {
    guard session == sessionId, session != 0, rimeAPI.find_session(session) else { return }
    guard let key = ReservedPropertyKey(rawValue: rawKey) else { throw .unknownInput(rawKey) }
    let parsed = try ReservedPropertyValue.parse(rawValue)
    switch key {
    case .commentHighlight:
      specialCommentIndices[.commentHighlight] = try parsed.indices()
    case .commentWarning:
      specialCommentIndices[.commentWarning] = try parsed.indices()
    case .refreshUI:
      rimeUpdate(clearReservedComments: false)
    }
  }

  deinit {
    voiceArmTimer?.invalidate()
    voiceHoldMonitorTimer?.invalidate()
    voiceStartWatchdog?.invalidate()
    voiceFinalWatchdog?.invalidate()
    if voiceActive {
      DistributedNotificationCenter.default().postNotificationName(
        Self.voiceStopNotification,
        object: nil,
        userInfo: ["session": voiceSessionID, "reason": "controller-deinit"],
        deliverImmediately: true
      )
    }
    DistributedNotificationCenter.default().removeObserver(self)
    destroySession()
  }
}

private extension SquirrelInputController {

  static func isPhysicalBackspace(_ event: NSEvent) -> Bool {
    VoiceFlyChaiziInteractionRules.isPhysicalBackspace(
      keyCode: Int(event.keyCode),
      character: event.charactersIgnoringModifiers?.first
    )
  }

  func directKeyboardSymbol(from event: NSEvent) -> String? {
    guard productSettings.directSymbols else { return nil }
    let shortcutModifiers = event.modifierFlags.intersection([.command, .control, .option])
    guard shortcutModifiers.isEmpty,
          let text = event.characters,
          text.unicodeScalars.count == 1,
          let scalar = text.unicodeScalars.first,
          scalar.value >= 0x21,
          scalar.value <= 0x7e else { return nil }

    let value = scalar.value
    let isDigit = value >= 0x30 && value <= 0x39
    let isUppercase = value >= 0x41 && value <= 0x5a
    let isLowercase = value >= 0x61 && value <= 0x7a
    return (isDigit || isUppercase || isLowercase) ? nil : text
  }

  func armVoiceInput(_ sender: Any!) {
    self.client ?= sender as? IMKTextInput
    guard !voiceActive else { return }
    voiceLog("arm")
    voiceArmTimer?.invalidate()
    voiceArmed = true
    let timer = Timer(timeInterval: VoiceFlyVoiceTiming.armDelay, repeats: false) { [weak self] _ in
      guard let self = self else { return }
      // flagsChanged releases are occasionally lost by IMK.  Never begin a
      // delayed voice session after the physical Control key is already up.
      guard self.physicalVoiceKeyIsDown else {
        self.cancelVoiceArm()
        return
      }
      self.voiceLog("arm fired")
      self.startVoiceInput()
    }
    voiceArmTimer = timer
    RunLoop.main.add(timer, forMode: .common)
  }

  func cancelVoiceArm() {
    voiceArmTimer?.invalidate()
    voiceArmTimer = nil
    voiceArmed = false
  }

  func startVoiceInput() {
    guard voiceArmed, !voiceActive, client != nil else { return }
    // Voice and chaizi both own the client's marked-text phase.  Leaving
    // chaizi active here lets a later Rime commit be captured as a component
    // and can strand the `~` marker when voice startup fails.
    if chaiziMode { exitChaiziMode() }
    cancelPhraseLearningState()
    cancelVoiceArm()
    if session != 0, rimeAPI.get_input(session) != nil {
      commitComposition(client)
    }
    voiceSessionID = UUID().uuidString
    voicePartialState.reset()
    voiceHoldReleaseDetector.reset()
    voiceActive = true
    voiceStopRequested = false
    startVoiceHoldMonitor()
    preedit = ""
    selRange = NSRange(location: 0, length: 0)
    caretPos = 0
    let connectingMarker = "…"
    show(
      preedit: connectingMarker,
      selRange: NSRange(location: 0, length: connectingMarker.utf16.count),
      caretPos: connectingMarker.utf16.count
    )
    let startedSession = voiceSessionID
    voiceStartWatchdog?.invalidate()
    voiceStartWatchdog = Timer.scheduledTimer(
      withTimeInterval: VoiceFlyVoiceTiming.clientStartFallbackTimeout,
      repeats: false
    ) { [weak self] _ in
      guard let self = self,
            self.voiceActive,
            self.voiceSessionID == startedSession else { return }
      self.voiceLog("service start timed out session=\(startedSession)")
      DistributedNotificationCenter.default().postNotificationName(
        Self.voiceStopNotification,
        object: nil,
        userInfo: ["session": startedSession, "reason": "client-start-timeout"],
        deliverImmediately: true
      )
      self.resetVoiceSession(clearMarkedText: true)
      self.deployLearnedPhrasesIfSafe()
      NSSound.beep()
    }
    voiceLog("start session=\(startedSession)")
    DistributedNotificationCenter.default().postNotificationName(
      Self.voiceStartNotification,
      object: nil,
      userInfo: ["session": startedSession],
      deliverImmediately: true
    )
  }

  func stopVoiceInput(reason: String) {
    guard voiceActive, !voiceStopRequested else { return }
    voiceHoldMonitorTimer?.invalidate()
    voiceHoldMonitorTimer = nil
    voiceStopRequested = true
    let stoppedSession = voiceSessionID
    voiceFinalWatchdog?.invalidate()
    voiceFinalWatchdog = Timer.scheduledTimer(withTimeInterval: 8.0, repeats: false) { [weak self] _ in
      guard let self = self,
            self.voiceActive,
            self.voiceSessionID == stoppedSession else { return }
      self.voiceLog("service final timed out session=\(stoppedSession)")
      self.resetVoiceSession(clearMarkedText: true)
      self.deployLearnedPhrasesIfSafe()
      NSSound.beep()
    }
    voiceLog("stop session=\(stoppedSession) reason=\(reason)")
    DistributedNotificationCenter.default().postNotificationName(
      Self.voiceStopNotification,
      object: nil,
      userInfo: ["session": stoppedSession, "reason": reason],
      deliverImmediately: true
    )
  }

  func cancelVoiceInput(reason: String) {
    guard voiceActive else { return }
    let cancelledSession = voiceSessionID
    voiceLog("cancel session=\(cancelledSession) reason=\(reason)")
    DistributedNotificationCenter.default().postNotificationName(
      Self.voiceStopNotification,
      object: nil,
      userInfo: ["session": cancelledSession, "reason": "cancel:\(reason)"],
      deliverImmediately: true
    )
    resetVoiceSession(clearMarkedText: true)
  }

  func resetVoiceSession(clearMarkedText: Bool) {
    cancelVoiceArm()
    voiceHoldMonitorTimer?.invalidate()
    voiceHoldMonitorTimer = nil
    voiceStartWatchdog?.invalidate()
    voiceStartWatchdog = nil
    voiceFinalWatchdog?.invalidate()
    voiceFinalWatchdog = nil
    voiceActive = false
    voiceStopRequested = false
    voiceSessionID = ""
    voicePartialState.reset()
    voiceHoldReleaseDetector.reset()
    if clearMarkedText {
      clearVoiceMarkedText()
    }
  }

  func startVoiceHoldMonitor() {
    voiceHoldMonitorTimer?.invalidate()
    let timer = Timer(
      timeInterval: VoiceFlyVoiceTiming.holdMonitorInterval,
      repeats: true
    ) { [weak self] _ in
      guard let self = self else { return }
      guard self.voiceActive, !self.voiceStopRequested else {
        self.voiceHoldMonitorTimer?.invalidate()
        self.voiceHoldMonitorTimer = nil
        return
      }
      if self.voiceHoldReleaseDetector.shouldStop(
        controlIsPhysicallyDown: self.physicalVoiceKeyIsDown
      ) {
        self.voiceLog("sustained physical Control release confirmed; stopping voice session")
        self.stopVoiceInput(reason: "control-state-missing-confirmed")
      }
    }
    voiceHoldMonitorTimer = timer
    RunLoop.main.add(timer, forMode: .common)
  }

  var physicalVoiceKeyIsDown: Bool {
    CGEventSource.flagsState(.combinedSessionState).contains(productSettings.voiceKey == "option" ? .maskAlternate : .maskControl)
  }

  func clearVoiceMarkedText() {
    if !preedit.isEmpty, let client = client {
      client.setMarkedText(
        NSMutableAttributedString(string: ""),
        selectionRange: NSRange(location: 0, length: 0),
        replacementRange: .empty
      )
    }
    preedit = ""
    selRange = NSRange(location: 0, length: 0)
    caretPos = 0
    hidePalettes()
  }

  func voiceLog(_ message: String) {
    let timestamp = String(format: "%.3f", ProcessInfo.processInfo.systemUptime)
    let line = "[VoiceFly][\(timestamp)] \(message)"
    print(line)
    NSLog("%@", line)
  }

  @objc func handleVoiceStarted(_ notification: Notification) {
    guard let info = notification.userInfo,
          let incomingSession = info["session"] as? String else { return }
    DispatchQueue.main.async { [weak self] in
      guard let self = self,
            self.voiceActive,
            incomingSession == self.voiceSessionID else { return }
      self.voiceLog("service accepted session=\(incomingSession)")
    }
  }

  @objc func handleVoiceMicReady(_ notification: Notification) {
    guard let info = notification.userInfo,
          let incomingSession = info["session"] as? String else { return }
    DispatchQueue.main.async { [weak self] in
      guard let self = self,
            self.voiceActive,
            !self.voiceStopRequested,
            incomingSession == self.voiceSessionID else { return }
      self.voiceStartWatchdog?.invalidate()
      self.voiceStartWatchdog = nil
      self.voiceLog("microphone ready session=\(incomingSession)")
      let marker = "🎙"
      self.show(
        preedit: marker,
        selRange: NSRange(location: 0, length: marker.utf16.count),
        caretPos: marker.utf16.count
      )
    }
  }

  @objc func handleVoicePartial(_ notification: Notification) {
    guard let info = notification.userInfo,
          let incomingSession = info["session"] as? String,
          let text = info["text"] as? String else { return }
    let sequence = (info["sequence"] as? NSNumber)?.intValue
    let stableUTF16 = (info["stable_utf16"] as? NSNumber)?.intValue
    DispatchQueue.main.async { [weak self] in
      guard let self = self,
            self.voiceActive,
            incomingSession == self.voiceSessionID else { return }
      self.voiceStartWatchdog?.invalidate()
      self.voiceStartWatchdog = nil
      guard let presentation = self.voicePartialState.accept(
        text: text,
        sequence: sequence,
        stableUTF16: stableUTF16
      ) else { return }
      let length = presentation.text.utf16.count
      self.show(
        preedit: presentation.text,
        selRange: presentation.mutableRange,
        caretPos: length
      )
    }
  }

  @objc func handleVoiceFinal(_ notification: Notification) {
    guard let info = notification.userInfo,
          let incomingSession = info["session"] as? String else { return }
    let text = info["text"] as? String ?? ""
    DispatchQueue.main.async { [weak self] in
      guard let self = self,
            self.voiceActive,
            incomingSession == self.voiceSessionID else { return }
      self.voiceStartWatchdog?.invalidate()
      self.voiceStartWatchdog = nil
      self.voiceFinalWatchdog?.invalidate()
      self.voiceFinalWatchdog = nil
      self.voiceHoldMonitorTimer?.invalidate()
      self.voiceHoldMonitorTimer = nil
      self.voiceActive = false
      self.voiceStopRequested = false
      self.voiceSessionID = ""
      self.voicePartialState.reset()
      if text.isEmpty {
        self.clearVoiceMarkedText()
      } else {
        self.cancelPhraseLearningState()
        // Always finish the marked-text phase before insertText. This also
        // makes final-only results reliable in Electron/WebView editors.
        let length = text.utf16.count
        self.show(
          preedit: text,
          selRange: NSRange(location: length, length: 0),
          caretPos: length
        )
        self.commit(string: text)
        self.recordRecentPhraseCommit(from: text)
      }
      self.voiceLog("final session=\(incomingSession) utf16=\(text.utf16.count)")
      self.deployLearnedPhrasesIfSafe()
    }
  }

  @objc func handleVoiceError(_ notification: Notification) {
    guard let info = notification.userInfo,
          let incomingSession = info["session"] as? String else { return }
    let message = info["message"] as? String ?? "语音输入失败，请检查麦克风权限并在设置中重启语音服务。"
    DispatchQueue.main.async { [weak self] in
      guard let self = self,
            self.voiceActive,
            incomingSession == self.voiceSessionID else { return }
      self.voiceLog("service error session=\(incomingSession)")
      SquirrelApplicationDelegate.showMessage(msgText: message)
      self.resetVoiceSession(clearMarkedText: true)
      self.deployLearnedPhrasesIfSafe()
      NSSound.beep()
    }
  }

  func onChordTimer(_: Timer) {
    var processedKeys = false
    if chordKeyCount > 0 && session != 0 {
      // Chord typing releases are synthesized after the configured timeout.
      for i in 0..<chordKeyCount {
        let handled = rimeAPI.process_key(session, Int32(chordKeyCodes[i]), Int32(chordModifiers[i] | kReleaseMask.rawValue))
        if handled {
          processedKeys = true
        }
      }
    }
    clearChord()
    if processedKeys {
      rimeUpdate()
    }
  }

  func updateChord(keycode: UInt32, modifiers: UInt32) {
    for i in 0..<chordKeyCount where chordKeyCodes[i] == keycode {
      return
    }
    if chordKeyCount >= Self.keyRollOver {
      return
    }
    chordKeyCodes[chordKeyCount] = keycode
    chordModifiers[chordKeyCount] = modifiers
    chordKeyCount += 1
    if let timer = chordTimer, timer.isValid {
      timer.invalidate()
    }
    chordDuration = 0.1
    if let duration = NSApp.squirrelAppDelegate.config?.getDouble("chord_duration"), duration > 0 {
      chordDuration = duration
    }
    chordTimer = Timer.scheduledTimer(withTimeInterval: chordDuration, repeats: false, block: onChordTimer)
  }

  func clearChord() {
    chordKeyCount = 0
    if let timer = chordTimer {
      if timer.isValid {
        timer.invalidate()
      }
      chordTimer = nil
    }
  }

  private func handlePhraseLearningSpace() -> Bool {
    guard productSettings.spaceLearning, !chaiziMode,
          !rimeAPI.get_option(session, "ascii_mode"),
          let rawInput = rimeAPI.get_input(session),
          String(cString: rawInput).isEmpty else { return false }
    registerPhraseLearningSpace(
      at: ProcessInfo.processInfo.systemUptime,
      fallbackSpaces: 1
    )
    return true
  }

  private func registerPhraseLearningSpace(
    at now: TimeInterval,
    fallbackSpaces: Int
  ) {
    guard productSettings.spaceLearning else { return }
    if let startedAt = phraseSpaceGesture.startedAt,
       now - startedAt > phraseSpaceGesture.timeout {
      settlePendingPhraseLearningGesture()
    }
    performPhraseLearningActions(
      phraseSpaceGesture.tap(
        at: now,
        fallbackSpaces: fallbackSpaces
      )
    )
    pendingPhraseLearningSpaceTimer?.invalidate()
    pendingPhraseLearningSpaceTimer = nil
    if let remaining = phraseSpaceGesture.remaining(at: now) {
      let timer = Timer(timeInterval: remaining, repeats: false) { [weak self] _ in
        guard let self else { return }
        self.pendingPhraseLearningSpaceTimer = nil
        self.settlePendingPhraseLearningGesture()
      }
      pendingPhraseLearningSpaceTimer = timer
      RunLoop.main.add(timer, forMode: .common)
    }
  }

  private func settlePendingPhraseLearningGesture(forKeyRepeat: Bool = false) {
    guard phraseSpaceGesture.tapCount > 0 else { return }
    pendingPhraseLearningSpaceTimer?.invalidate()
    pendingPhraseLearningSpaceTimer = nil
    performPhraseLearningActions(forKeyRepeat
      ? phraseSpaceGesture.settleForKeyRepeat()
      : phraseSpaceGesture.settle())
  }

  private func cancelPhraseLearningState() {
    pendingPhraseLearningSpaceTimer?.invalidate()
    pendingPhraseLearningSpaceTimer = nil
    cancelPhraseLengthSelection(clearLearningContext: false)
    phraseSpaceGesture.cancel()
    phraseLearningJournal.reset()
    phraseCommitInputHint = nil
    recentPhraseCommit.invalidate()
  }

  private func performPhraseLearningActions(_ actions: [VoiceFlySpaceGestureAction]) {
    for action in actions {
      switch action {
      case let .insertSpaces(count):
        commit(string: String(repeating: " ", count: count), establishMarkedText: true)
        phraseLearningJournal.reset()
        recentPhraseCommit.invalidate()
      case let .requestPhraseLength(fallbackSpaces):
        beginPhraseLengthSelection(fallbackSpaces: fallbackSpaces)
      case .enterChaizi:
        phraseLearningJournal.reset()
        recentPhraseCommit.invalidate()
        enterChaiziMode()
      }
    }
    deployLearnedPhrasesIfSafe()
  }

  private func beginPhraseLengthSelection(fallbackSpaces: Int) {
    phraseLengthSelectionTimer?.invalidate()
    phraseLengthSelectionTimer = nil
    phraseLengthChoices.removeAll(keepingCapacity: true)

    let visiblePrefix = visiblePrefixBeforeCaret(maxUTF16Length: 24)
    for length in 2...5 {
      if let draft = Self.pinyinLexicon.draft(
        fromVisiblePrefix: visiblePrefix,
        exactLength: length,
        journalDraft: phraseLearningJournal.draft(length: length)
      ) {
        phraseLengthChoices[length] = draft
      }
    }
    if phraseLengthChoices.isEmpty,
       visiblePrefix == nil,
       let recent = recentPhraseCommit.validDraft(
         at: ProcessInfo.processInfo.systemUptime
       ),
       (2...5).contains(recent.text.count) {
      phraseLengthChoices[recent.text.count] = recent
    }

    guard !phraseLengthChoices.isEmpty else {
      if fallbackSpaces > 0 {
        commit(
          string:
          String(repeating: " ", count: fallbackSpaces),
          establishMarkedText: true
        )
      }
      phraseLearningJournal.reset()
      recentPhraseCommit.invalidate()
      SquirrelApplicationDelegate.showMessage(
        msgText: "未进入热词录入：光标前没有可保存的连续中文（2–5 字）"
      )
      return
    }

    showPhraseLengthSelectionPrompt()
    let timer = Timer(timeInterval: 5, repeats: false) { [weak self] _ in
      guard let self, !self.phraseLengthChoices.isEmpty else { return }
      self.phraseLengthSelectionTimer = nil
      self.phraseLengthChoices.removeAll(keepingCapacity: true)
      self.phraseLearningJournal.reset()
      self.recentPhraseCommit.invalidate()
      SquirrelApplicationDelegate.showMessage(msgText: "热词录入已取消：未选择字数")
    }
    phraseLengthSelectionTimer = timer
    RunLoop.main.add(timer, forMode: .common)
  }

  private func showPhraseLengthSelectionPrompt() {
    let options = phraseLengthChoices.keys.sorted().compactMap { length -> String? in
      guard let draft = phraseLengthChoices[length] else { return nil }
      return "\(length)=\(draft.text)"
    }.joined(separator: "  ")
    SquirrelApplicationDelegate.showMessage(
      msgText: "选择热词字数：\(options)；按 Esc 取消"
    )
  }

  private func cancelPhraseLengthSelection(clearLearningContext: Bool) {
    phraseLengthSelectionTimer?.invalidate()
    phraseLengthSelectionTimer = nil
    phraseLengthChoices.removeAll(keepingCapacity: true)
    if clearLearningContext {
      phraseLearningJournal.reset()
      recentPhraseCommit.invalidate()
    }
  }

  private func handlePhraseLengthSelectionKeyDown(_ event: NSEvent) -> Bool {
    guard !phraseLengthChoices.isEmpty else { return false }
    let isEscape = event.keyCode == 53 ||
      event.charactersIgnoringModifiers?.first == "\u{1b}"
    if isEscape {
      cancelPhraseLengthSelection(clearLearningContext: true)
      SquirrelApplicationDelegate.showMessage(msgText: "已取消热词录入")
      return true
    }

    let disallowedModifiers = event.modifierFlags.intersection([
      .command, .control, .option, .shift,
    ])
    if disallowedModifiers.isEmpty,
       let character = event.charactersIgnoringModifiers?.first,
       let length = Int(String(character)),
       (2...5).contains(length) {
      guard let selected = phraseLengthChoices[length] else {
        showPhraseLengthSelectionPrompt()
        return true
      }
      let refreshed = visiblePhraseLearningDraft(
        exactLength: length,
        allowRecentFallback: true
      )
      guard refreshed == selected else {
        cancelPhraseLengthSelection(clearLearningContext: true)
        SquirrelApplicationDelegate.showMessage(msgText: "未学习：光标前文字已经变化")
        return true
      }
      cancelPhraseLengthSelection(clearLearningContext: false)
      learnPhrase(selected)
      return true
    }

    // A non-selection key ends the modal state but continues through the normal
    // input path, so ordinary typing is never swallowed.
    cancelPhraseLengthSelection(clearLearningContext: true)
    return false
  }

  private func visiblePrefixBeforeCaret(maxUTF16Length: Int = 16) -> String? {
    guard let client else { return nil }
    let selection = client.selectedRange()
    guard selection.location != NSNotFound,
          selection.length == 0,
          selection.location > 0,
          maxUTF16Length > 0 else { return nil }

    // A supplementary-plane Han glyph occupies two UTF-16 code units. The
    // largest proposed range can therefore begin between a surrogate pair in
    // the preceding text. Retry shorter windows until the host accepts a
    // grapheme-safe boundary. Nil for every range means that this client does
    // not expose verifiable surrounding text, so learning must fail closed.
    let longest = min(selection.location, maxUTF16Length)
    for length in stride(from: longest, through: 1, by: -1) {
      let range = NSRange(location: selection.location - length, length: length)
      if let text = client.attributedSubstring(from: range)?.string {
        return text
      }
    }
    return nil
  }

  private func visiblePhraseLearningDraft(
    exactLength: Int,
    allowRecentFallback: Bool
  ) -> VoiceFlyPhraseDraft? {
    if let visibleDraft = Self.pinyinLexicon.draft(
      fromVisiblePrefix: visiblePrefixBeforeCaret(),
      exactLength: exactLength,
      journalDraft: phraseLearningJournal.draft(length: exactLength)
    ) {
      return visibleDraft
    }
    guard allowRecentFallback else { return nil }
    let recent = recentPhraseCommit.validDraft(at: ProcessInfo.processInfo.systemUptime)
    return recent?.text.count == exactLength ? recent : nil
  }

  private func recordRecentPhraseCommit(from text: String) {
    phraseLearningJournal.reset()
    guard let draft = Self.pinyinLexicon.draft(fromVisiblePrefix: text) else {
      recentPhraseCommit.invalidate()
      return
    }
    recentPhraseCommit.record(draft, at: ProcessInfo.processInfo.systemUptime)
  }

  private func updatePhraseLearningJournalAfterKey(
    char: Character,
    keycode: UInt32,
    modifiers: UInt32,
    hadComposition: Bool,
    handled: Bool
  ) {
    guard !chaiziMode else { return }
    if keycode == UInt32(XK_BackSpace), modifiers == 0 {
      // Rime-consumed Backspace edits only the unfinished composition. An
      // unhandled Backspace reaches the host document and removes one of the
      // already committed glyphs.
      if !handled && !hadComposition {
        _ = phraseLearningJournal.deleteBackward()
      } else if !handled {
        phraseLearningJournal.reset()
      }
      return
    }
    if !handled || modifiers != 0 || (char.isWhitespace && !hadComposition) {
      phraseLearningJournal.reset()
    }
  }

  private func enterChaiziMode() {
    phraseLearningJournal.reset()
    chaiziMode = true
    chaiziComponents.removeAll()
    chaiziResults.removeAll()
    chaiziHighlightedIndex = 0
    chaiziQuerySpaceCount = 0
    chaiziQuerySpaceStartedAt = nil
    if session != 0, rimeAPI.find_session(session) {
      rimeAPI.clear_composition(session)
    }
    hidePalettes()
    showChaiziMarker()
    SquirrelApplicationDelegate.showMessage(
      msgText: "~ 拆字模式：输入 1–4 个部件；退格撤销，空状态三击空格、~ 或 Esc 退出"
    )
  }

  private func exitChaiziMode() {
    chaiziMode = false
    chaiziComponents.removeAll()
    chaiziResults.removeAll()
    chaiziHighlightedIndex = 0
    chaiziQuerySpaceCount = 0
    chaiziQuerySpaceStartedAt = nil
    if session != 0, rimeAPI.find_session(session) {
      rimeAPI.clear_composition(session)
    }
    // Manual chaizi candidates keep an IMK marked-text placeholder active so
    // WebView/Electron clients continue routing Tab and Escape to the input
    // method.  Remove it without committing the placeholder on every exit.
    if !preedit.isEmpty {
      client?.setMarkedText(
        NSMutableAttributedString(string: ""),
        selectionRange: NSRange(location: 0, length: 0),
        replacementRange: .empty
      )
      preedit = ""
      selRange = NSRange(location: 0, length: 0)
      caretPos = 0
    }
    hidePalettes()
  }

  private func handleChaiziKeyDown(_ event: NSEvent) -> Bool {
    guard chaiziMode else { return false }
    let isEscape = event.keyCode == 53 || event.charactersIgnoringModifiers?.first == "\u{1b}"
    if isEscape {
      exitChaiziMode()
      SquirrelApplicationDelegate.showMessage(msgText: "已退出拆字模式")
      return true
    }

    if VoiceFlyChaiziInteractionRules.isTildeExit(event.characters?.first) {
      exitChaiziMode()
      SquirrelApplicationDelegate.showMessage(msgText: "已退出拆字模式")
      return true
    }

    let keyCharacter = event.charactersIgnoringModifiers?.first
    if Self.isPhysicalBackspace(event) {
      handleChaiziBackspace()
      return true
    }

    if !chaiziResults.isEmpty {
      let isDownArrow = event.keyCode == 125 || keyCharacter == "\u{f701}"
      let isUpArrow = event.keyCode == 126 || keyCharacter == "\u{f700}"
      if isDownArrow {
        let nextRow = chaiziHighlightedIndex + 5
        chaiziHighlightedIndex = nextRow < chaiziResults.count ? nextRow : 0
        showChaiziResults()
        return true
      }
      if isUpArrow {
        chaiziHighlightedIndex = chaiziHighlightedIndex >= 5
          ? chaiziHighlightedIndex - 5
          : ((chaiziResults.count - 1) / 5) * 5
        showChaiziResults()
        return true
      }
      if event.keyCode == 48 || keyCharacter == "\t" || keyCharacter == "\u{19}" { // Tab / Backtab
        // Tab is deliberately disabled in chaizi results.  Some host apps
        // reserve it for focus traversal, so row navigation uses Up/Down.
        return true
      }
      if let char = event.charactersIgnoringModifiers?.first,
         let digit = char.wholeNumberValue, (1...5).contains(digit) {
        let rowStart = (chaiziHighlightedIndex / 5) * 5
        let index = rowStart + digit - 1
        if index < chaiziResults.count {
          commit(string: chaiziResults[index])
          exitChaiziMode()
        }
        return true
      }
      if event.keyCode == 36 || event.keyCode == 76 { // Return / keypad Enter
        commit(string: chaiziResults[chaiziHighlightedIndex])
        exitChaiziMode()
        return true
      }
      // Starting any other normal input cancels the result picker and lets
      // this same key continue through the regular Rime path.  Previously it
      // was swallowed here, making the input method appear frozen.
      exitChaiziMode()
      return false
    }

    let isPlainSpace = event.charactersIgnoringModifiers?.first == " " &&
      event.modifierFlags.intersection([.command, .control, .option]).isEmpty
    if isPlainSpace {
      // Repeats must not query or exit the manual mode as a triple tap.
      if event.isARepeat { return true }
      let hasRawInput = rimeAPI.get_input(session).map { !String(cString: $0).isEmpty } ?? false
      if !hasRawInput {
        return handleChaiziQuerySpace()
      }
    }
    // Any component typing/selection between query-space taps starts a fresh
    // three-space gesture.
    chaiziQuerySpaceCount = 0
    chaiziQuerySpaceStartedAt = nil

    if event.keyCode == 36 || event.keyCode == 76 {
      // Some host apps leave the last component in Rime's composition even
      // after Space.  Finalize it locally before querying and always consume
      // Return; never leak Return to a chat app where it may send a message.
      if let rawInput = rimeAPI.get_input(session), !String(cString: rawInput).isEmpty {
        let componentCount = chaiziComponents.count
        _ = processKey(UInt32(XK_space), modifiers: 0)
        rimeUpdate()
        if let remainingInput = rimeAPI.get_input(session), !String(cString: remainingInput).isEmpty {
          rimeAPI.clear_composition(session)
          hidePalettes()
        }
        if chaiziComponents.count == componentCount {
            SquirrelApplicationDelegate.showMessage(msgText: "最后一个部件未能确定，请重新选择；按 ~ 或 Esc 退出")
          return true
        }
      }
      presentChaiziCandidates()
      return true
    }
    return false
  }

  private func handleChaiziBackspace() {
    let hasRawInput = rimeAPI.get_input(session).map { !String(cString: $0).isEmpty } ?? false
    let action = VoiceFlyChaiziInteractionRules.backspaceAction(
      hasRawInput: hasRawInput,
      componentCount: chaiziComponents.count,
      hasResults: !chaiziResults.isEmpty
    )
    switch action {
    case .deleteRawInput:
      // Normalize every modifier variant to one composition-local Backspace.
      // Consume it even if Rime declines, so it can never reach host text.
      _ = processKey(UInt32(XK_BackSpace), modifiers: 0)
      rimeUpdate()
    case .returnToComponents:
      chaiziResults.removeAll()
      chaiziHighlightedIndex = 0
      showChaiziMarker()
      SquirrelApplicationDelegate.showMessage(
        msgText: "已返回部件输入：" + chaiziComponents.joined(separator: "+")
      )
    case .removeLastComponent:
      chaiziComponents.removeLast()
      showChaiziMarker()
      let detail = chaiziComponents.isEmpty
        ? "已撤销全部部件；再按退格、三击空格、~ 或 Esc 退出"
        : "拆字部件：" + chaiziComponents.joined(separator: "+")
      SquirrelApplicationDelegate.showMessage(msgText: detail)
    case .exit:
      exitChaiziMode()
      SquirrelApplicationDelegate.showMessage(msgText: "已退出拆字模式")
    }
  }

  private func handleChaiziQuerySpace() -> Bool {
    let now = Date()
    if chaiziQuerySpaceStartedAt.map({ now.timeIntervalSince($0) > 1 }) ?? true {
      chaiziQuerySpaceStartedAt = now
      chaiziQuerySpaceCount = 0
    }
    chaiziQuerySpaceCount += 1
    if chaiziQuerySpaceCount >= 3 {
      chaiziQuerySpaceCount = 0
      chaiziQuerySpaceStartedAt = nil
      if chaiziComponents.isEmpty {
        exitChaiziMode()
        SquirrelApplicationDelegate.showMessage(msgText: "已退出拆字模式")
      } else {
        presentChaiziCandidates()
      }
    } else {
      SquirrelApplicationDelegate.showMessage(
        msgText: "三击空格\(chaiziComponents.isEmpty ? "退出" : "查询")：\(chaiziQuerySpaceCount)/3"
      )
    }
    return true
  }

  private func presentChaiziCandidates() {
    guard !chaiziComponents.isEmpty else {
      SquirrelApplicationDelegate.showMessage(msgText: "请先输入拆字部件；退格、三击空格、~ 或 Esc 退出")
      return
    }
    chaiziResults = lookupChaizi(components: chaiziComponents)
    chaiziHighlightedIndex = 0
    if chaiziResults.isEmpty {
      exitChaiziMode()
      SquirrelApplicationDelegate.showMessage(msgText: "没有找到匹配汉字，已退出拆字模式")
    } else {
      showChaiziResults()
    }
  }

  private func showChaiziResults() {
    guard !chaiziResults.isEmpty else { return }
    // SquirrelPanel is a manual UI and, by itself, does not establish an IMK
    // composition.  Keep a visible marker in the client so apps continue
    // routing Up/Down, number keys and Escape to the input method instead of
    // treating them as editor navigation.
    showChaiziMarker()
    // Only hand the current five-item row to SquirrelPanel.  The panel's
    // highlighted index is local to the array it renders; passing a global
    // index such as 5/10/15 made the highlight point outside a collapsed row
    // and could crash TextKit while the panel was repainting.
    let rowStart = min((chaiziHighlightedIndex / 5) * 5, chaiziResults.count - 1)
    let rowEnd = min(rowStart + 5, chaiziResults.count)
    let visibleResults = Array(chaiziResults[rowStart..<rowEnd])
    let comments = visibleResults.map { result -> String in
      guard let char = result.first else { return "" }
      let readings = pinyinReadings()[char] ?? []
      return readings.isEmpty ? "" : "〔" + readings.joined(separator: "/") + "〕"
    }
    let rowNumber = rowStart / 5 + 1
    let rowCount = (chaiziResults.count + 4) / 5
    showPanel(preedit: "拆字：" + chaiziComponents.joined(separator: "+") + "（\(rowNumber)/\(rowCount)，↑↓换行，退格返回，~ / Esc 退出）",
              selRange: .empty, caretPos: 0,
              candidates: visibleResults, comments: comments,
              labels: ["1", "2", "3", "4", "5"],
              highlighted: 0, page: 0, lastPage: true)
  }

  private func showChaiziMarker() {
    show(
      preedit: Self.chaiziMarker,
      selRange: NSRange(location: 0, length: 0),
      caretPos: Self.chaiziMarker.utf16.count
    )
  }

  private func restoreChaiziPresentationAfterRimeUpdate() {
    guard chaiziMode else { return }
    let hasRawInput = rimeAPI.get_input(session).map { !String(cString: $0).isEmpty } ?? false
    guard VoiceFlyChaiziInteractionRules.shouldRestoreMarker(
      isActive: chaiziMode,
      hasRawInput: hasRawInput
    ) else { return }
    if chaiziResults.isEmpty {
      showChaiziMarker()
    } else {
      showChaiziResults()
    }
  }

  private func pinyinReadings() -> [Character: [String]] {
    Self.pinyinLexicon.characterReadings
  }

  private static func loadPinyinLexicon() -> VoiceFlyPinyinLexicon {
    var result = VoiceFlyPinyinLexicon()
    let files = [
      SquirrelApp.userDir.appendingPathComponent("terra_pinyin.dict.yaml"),
      URL(fileURLWithPath: Bundle.main.sharedSupportPath!).appendingPathComponent("pinyin_simp.dict.yaml")
    ]
    for url in files {
      if let content = try? String(contentsOf: url, encoding: .utf8) {
        for line in content.split(whereSeparator: \.isNewline) where !line.hasPrefix("#") {
          let fields = line.split(separator: "\t", omittingEmptySubsequences: false)
          guard fields.count >= 2 else { continue }
          result.add(text: String(fields[0]), pinyin: String(fields[1]))
        }
      }
    }
    return result
  }

  private static func loadChaiziIndex() -> VoiceFlyChaiziIndex {
    let url = SquirrelApp.userDir.appendingPathComponent("chaizi_ids.txt")
    var definitions: [Character: [Character]] = [:]
    if let content = try? String(contentsOf: url, encoding: .utf8) {
      for line in content.split(whereSeparator: \.isNewline) where !line.hasPrefix("#") {
        let fields = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
        guard fields.count >= 3, fields[1].count == 1,
              let target = fields[1].first else { continue }
        let description = fields[2].split(separator: "[").first.map(String.init) ?? fields[2]
        let parts = description.compactMap { char -> Character? in
          guard let scalar = char.unicodeScalars.first else { return nil }
          return (0x2FF0...0x2FFF).contains(Int(scalar.value)) ? nil : char
        }
        if !parts.isEmpty {
          definitions[target] = parts
        }
      }
    }
    return VoiceFlyChaiziIndex(definitions: definitions)
  }

  private func lookupChaizi(components: [String]) -> [String] {
    Self.chaiziIndex.lookup(components: components)
  }

  private func learnPhrase(_ draft: VoiceFlyPhraseDraft) {
    // Re-read the host immediately before enqueueing the write. The local
    // commit journal is deliberately not an eligibility signal: whole-word or
    // per-character commits, repeated Backspace, paste, undo, and other edit
    // histories all converge on the same final visible suffix.
    let visibleTextMatches = VoiceFlyVisiblePhraseExtractor.matchesVisibleSuffix(
      text: draft.text,
      visiblePrefix: visiblePrefixBeforeCaret()
    )
    let trustedRecentCommitMatches = recentPhraseCommit.validDraft(
      at: ProcessInfo.processInfo.systemUptime
    ) == draft
    guard visibleTextMatches || trustedRecentCommitMatches else {
      phraseLearningJournal.reset()
      recentPhraseCommit.invalidate()
      SquirrelApplicationDelegate.showMessage(msgText: "未学习：光标前文字已经变化")
      return
    }
    phraseLearningJournal.reset()
    recentPhraseCommit.invalidate()
    SquirrelApplicationDelegate.showMessage(msgText: "正在保存：\(draft.text)")
    Self.learnedPhraseWriteGroup.enter()
    Self.learnedPhraseWriteQueue.async {
      defer { Self.learnedPhraseWriteGroup.leave() }
      do {
        let changed = try Self.learnedPhraseStore.save(phrase: draft.text, code: draft.code)
        Self.recordPersonalUsage(text: draft.text, code: draft.code, explicit: true)
        guard Self.personalUsageStore.flushSynchronously(timeout: 2) else {
          throw VoiceFlySettingsError.invalid("个人词库尚未保存")
        }
        try Self.wordManager.regenerate()
        DispatchQueue.main.async { [weak self] in
          let status = changed ? "已保存" : "已确认"
          SquirrelApplicationDelegate.showMessage(msgText: "\(status)：\(draft.text)，已排队载入词库")
          // Deploy even for an existing row.  A previous deploy may have been
          // interrupted; deduplication of the file is not proof that the
          // current Rime generation has loaded it.
          Self.learnedPhraseDeployPending = true
          (Self.activeController ?? self)?.deployLearnedPhrasesIfSafe()
        }
      } catch {
        print("Unable to save learned phrase; original preserved.")
        DispatchQueue.main.async {
          SquirrelApplicationDelegate.showMessage(msgText: "学习失败：原词库未改动")
          NSSound.beep()
        }
      }
    }
  }

  private func deployLearnedPhrasesIfSafe() {
    guard Self.learnedPhraseDeployPending else { return }
    // Maintenance tears down every Rime session, not only the controller that
    // initiated the write.  An async save can finish after focus has moved to a
    // different application, so only the currently active controller may
    // authorize deployment.
    if let activeController = Self.activeController, activeController !== self {
      activeController.deployLearnedPhrasesIfSafe()
      return
    }
    let hasValidSession = session != 0 && rimeAPI.find_session(session)
    let hasRawInput = hasValidSession && (
      rimeAPI.get_input(session).map { !String(cString: $0).isEmpty } ?? false
    )
    guard VoiceFlyLearnedPhraseDeploymentRules.isSafe(
      hasValidSession: hasValidSession,
      voiceArmed: voiceArmed,
      voiceActive: voiceActive,
      chaiziMode: chaiziMode,
      pendingSpaceTapCount: phraseSpaceGesture.tapCount,
      hasRawInput: hasRawInput
    ) else { return }
    guard !Self.learnedPhraseDeployScheduled else { return }
    Self.learnedPhraseDeployScheduled = true
    // Let the current IMK callback return before maintenance tears down the
    // Rime session. Focus or composition may change before this block runs, so
    // re-check the then-active controller and keep the request pending if it is
    // no longer safe.
    DispatchQueue.main.async {
      Self.learnedPhraseDeployScheduled = false
      guard Self.learnedPhraseDeployPending else { return }
      if let activeController = Self.activeController {
        let activeHasValidSession = activeController.session != 0 &&
          activeController.rimeAPI.find_session(activeController.session)
        let activeHasRawInput = activeHasValidSession && (
          activeController.rimeAPI.get_input(activeController.session).map {
            !String(cString: $0).isEmpty
          } ?? false
        )
        guard VoiceFlyLearnedPhraseDeploymentRules.isSafe(
          hasValidSession: activeHasValidSession,
          voiceArmed: activeController.voiceArmed,
          voiceActive: activeController.voiceActive,
          chaiziMode: activeController.chaiziMode,
          pendingSpaceTapCount: activeController.phraseSpaceGesture.tapCount,
          hasRawInput: activeHasRawInput
        ) else { return }
      }
      Self.learnedPhraseDeployPending = false
      NSApp.squirrelAppDelegate.deploy()
    }
  }

  func createSession() {
    if chaiziMode { exitChaiziMode() }
    cancelPhraseLearningState()
    let app = client?.bundleIdentifier() ?? {
      SquirrelInputController.unknownAppCnt &+= 1
      return "UnknownApp\(SquirrelInputController.unknownAppCnt)"
    }()
    print("createSession: \(app)")
    currentApp = app
    session = rimeAPI.create_session()
    schemaId = ""
    productSettings = (try? VoiceFlySettingsStore().load()) ?? productSettings
    if session != 0, FileManager.default.fileExists(atPath: VoiceFlyPaths().settings.path) {
      productSettings.schema.rimeID.withCString { _ = rimeAPI.select_schema(session, $0) }
      rimeAPI.set_option(session, "simplification", productSettings.simplified)
      rimeAPI.set_option(session, "full_shape", productSettings.fullShape)
    }

    if session != 0 {
      updateAppOptions()
    }
  }

  func updateAppOptions() {
    if currentApp == "" {
      return
    }
    if let appOptions = NSApp.squirrelAppDelegate.config?.getAppOptions(currentApp) {
      for (key, value) in appOptions {
        print("set app option: \(key) = \(value)")
        rimeAPI.set_option(session, key, value)
      }
    }
    if let reportBundleID = NSApp.squirrelAppDelegate.config?.getBool("unsafe/report_bundleid"), reportBundleID {
      currentApp.withCString { name in
        rimeAPI.set_property(session, "client_app", name)
      }
    }
  }

  func destroySession() {
    cancelPhraseLearningState()
    if session != 0 {
      _ = rimeAPI.destroy_session(session)
      session = 0
    }
    clearChord()
  }

  func processKey(_ rimeKeycode: UInt32, modifiers rimeModifiers: UInt32) -> Bool {
    if let panel = NSApp.squirrelAppDelegate.panel {
      if panel.linear != rimeAPI.get_option(session, "_linear") {
        rimeAPI.set_option(session, "_linear", panel.linear)
      }
      if panel.vertical != rimeAPI.get_option(session, "_vertical") {
        rimeAPI.set_option(session, "_vertical", panel.vertical)
      }
    }

    let handled = rimeAPI.process_key(session, Int32(rimeKeycode), Int32(rimeModifiers))

    if !handled {
      let isVimBackInCommandMode = rimeKeycode == XK_Escape || ((rimeModifiers & kControlMask.rawValue != 0) && (rimeKeycode == XK_c || rimeKeycode == XK_C || rimeKeycode == XK_bracketleft))
      if isVimBackInCommandMode && rimeAPI.get_option(session, "vim_mode") &&
          !rimeAPI.get_option(session, "ascii_mode") {
        rimeAPI.set_option(session, "ascii_mode", true)
      }
    } else {
      let isChordingKey = switch Int32(rimeKeycode) {
      case XK_space...XK_asciitilde, XK_Control_L, XK_Control_R, XK_Alt_L, XK_Alt_R, XK_Shift_L, XK_Shift_R:
        true
      default:
        false
      }
      if isChordingKey && rimeAPI.get_option(session, "_chord_typing") {
        updateChord(keycode: rimeKeycode, modifiers: rimeModifiers)
      } else if (rimeModifiers & kReleaseMask.rawValue) == 0 {
        clearChord()
      }
    }

    return handled
  }

  func rimeConsumeCommittedText() {
    var commitText = RimeCommit.rimeStructInit()
    if rimeAPI.get_commit(session, &commitText) {
      if let text = commitText.text {
        let value = String(cString: text)
        if chaiziMode && chaiziResults.isEmpty {
          if value.count == 1 && chaiziComponents.count < 4 {
            chaiziComponents.append(value)
            showChaiziMarker()
            SquirrelApplicationDelegate.showMessage(
              msgText: "拆字部件：" + chaiziComponents.joined(separator: "+") + "；三击空格查询，退格撤销"
            )
          } else {
            SquirrelApplicationDelegate.showMessage(msgText: "拆字模式每次请选择一个部件，最多四个")
          }
        } else {
          let committedCode = phraseLearningJournal.recordCommitted(
            text: value,
            inputHint: ["double_pinyin_flypy", "voicefly_flypy"].contains(schemaId) ? phraseCommitInputHint : nil,
            readings: pinyinReadings()
          )
          if productSettings.personalLearning, let code = committedCode, (2...12).contains(value.count) {
            Self.recordPersonalUsage(text: value, code: code, explicit: false)
          }
          if let code = committedCode, (2...5).contains(value.count) {
            recentPhraseCommit.record(
              VoiceFlyPhraseDraft(text: value, code: code),
              at: ProcessInfo.processInfo.systemUptime
            )
          } else if let draft = phraseLearningJournal.draft {
            recentPhraseCommit.record(
              draft,
              at: ProcessInfo.processInfo.systemUptime
            )
          } else {
            recentPhraseCommit.invalidate()
          }
          commit(string: value)
        }
      }
      _ = rimeAPI.free_commit(&commitText)
    }
  }

  // Preserve reserved comment marks when librime requests a UI-only refresh.
  func rimeUpdate(clearReservedComments: Bool = true) {
    // Empty Rime context is allowed to clear its own preedit below.  Reassert
    // the manual chaizi composition afterwards so component commits and the
    // last raw-input Backspace cannot erase the visible `~` marker.
    defer { restoreChaiziPresentationAfterRimeUpdate() }
    if clearReservedComments {
      specialCommentIndices = [:]
    }
    rimeConsumeCommittedText()

    var status = RimeStatus_stdbool.rimeStructInit()
    if rimeAPI.get_status(session, &status) {
      // swiftlint:disable:next identifier_name
      if let schema_id = status.schema_id, schemaId == "" || schemaId != String(cString: schema_id) {
        schemaId = String(cString: schema_id)
        NSApp.squirrelAppDelegate.loadSettings(for: schemaId)
        if let panel = NSApp.squirrelAppDelegate.panel {
          inlinePreedit = (panel.inlinePreedit && !rimeAPI.get_option(session, "no_inline")) || rimeAPI.get_option(session, "inline")
          inlineCandidate = panel.inlineCandidate && !rimeAPI.get_option(session, "no_inline")
          rimeAPI.set_option(session, "soft_cursor", !inlinePreedit)
        }
      }
      _ = rimeAPI.free_status(&status)
    }

    var ctx = RimeContext_stdbool.rimeStructInit()
    if rimeAPI.get_context(session, &ctx) {
      let preedit = ctx.composition.preedit.map({ String(cString: $0) }) ?? ""

      let start = String.Index(preedit.utf8.index(preedit.utf8.startIndex, offsetBy: Int(ctx.composition.sel_start)), within: preedit) ?? preedit.startIndex
      let end = String.Index(preedit.utf8.index(preedit.utf8.startIndex, offsetBy: Int(ctx.composition.sel_end)), within: preedit) ?? preedit.startIndex
      let caretPos = String.Index(preedit.utf8.index(preedit.utf8.startIndex, offsetBy: Int(ctx.composition.cursor_pos)), within: preedit) ?? preedit.startIndex

      if inlineCandidate {
        var candidatePreview = ctx.commit_text_preview.map { String(cString: $0) } ?? ""
        let endOfCandidatePreview = candidatePreview.endIndex
        if inlinePreedit {
          // 左移光標後的情形：
          // preedit:             ^已選某些字[xiang zuo yi dong]|guangbiao$
          // commit_text_preview: ^已選某些字向左移動$
          // candidate_preview:   ^已選某些字[向左移動]|guangbiao$
          // 繼續翻頁至指定更短字詞的情形：
          // preedit:             ^已選某些字[xiang zuo]yidong|guangbiao$
          // commit_text_preview: ^已選某些字向左yidong$
          // candidate_preview:   ^已選某些字[向左]yidong|guangbiao$
          // 光標移至當前段落最左端的情形：
          // preedit:             ^已選某些字|[xiang zuo yi dong guang biao]$
          // commit_text_preview: ^已選某些字向左移動光標$
          // candidate_preview:   ^已選某些字|[向左移動光標]$
          // 討論：
          // preedit 與 commit_text_preview 中“已選某些字”部分一致
          // 因此，選中範圍即正在翻譯的碼段“向左移動”中，兩者的 start 值一致
          // 光標位置的範圍是 start ..= endOfCandidatePreview
          if caretPos >= end && caretPos < preedit.endIndex {
            // 從 preedit 截取光標後未翻譯的編碼“guangbiao”
            candidatePreview += preedit[caretPos...]
          }
        } else {
          // 翻頁至指定更短字詞的情形：
          // preedit:             ^已選某些字[xiang zuo]yidong|guangbiao$
          // commit_text_preview: ^已選某些字向左yidongguangbiao$
          // candidate_preview:   ^已選某些字[向左???]|$
          // 光標移至當前段落最左端，繼續翻頁至指定更短字詞的情形：
          // preedit:             ^已選某些字|[xiang zuo]yidongguangbiao$
          // commit_text_preview: ^已選某些字向左yidongguangbiao$
          // candidate_preview:   ^已選某些字|[向左]???$
          // FIXME: add librime APIs to support preview candidate without remaining code.
        }
        // preedit can contain additional prompt text before start:
        // ^(prompt)[selection]$
        let start = min(start, candidatePreview.endIndex)
        let caretPos = caretPos <= start ? caretPos : endOfCandidatePreview
        show(preedit: candidatePreview,
             selRange: NSRange(location: start.utf16Offset(in: candidatePreview),
                               length: candidatePreview.utf16.distance(from: start, to: candidatePreview.endIndex)),
             caretPos: caretPos.utf16Offset(in: candidatePreview))
      } else {
        if inlinePreedit {
          show(preedit: preedit, selRange: NSRange(location: start.utf16Offset(in: preedit), length: preedit.utf16.distance(from: start, to: end)), caretPos: caretPos.utf16Offset(in: preedit))
        } else {
          // Use a full-width space placeholder to prevent iTerm2 from echoing raw preedit;
          // half-width placeholders make the Chinese composition baseline unstable.
          show(preedit: preedit.isEmpty ? "" : "　", selRange: NSRange(location: 0, length: 0), caretPos: 0)
        }
      }

      let numCandidates = Int(ctx.menu.num_candidates)
      var candidates = [String]()
      var comments = [String]()
      for i in 0..<numCandidates {
        let candidate = ctx.menu.candidates[i]
        candidates.append(candidate.text.map { String(cString: $0) } ?? "")
        comments.append(candidate.comment.map { String(cString: $0) } ?? "")
      }
      var labels = [String]()
      // swiftlint:disable identifier_name
      if let select_keys = ctx.menu.select_keys {
        labels = String(cString: select_keys).map { String($0) }
      } else if let select_labels = ctx.select_labels {
        let pageSize = Int(ctx.menu.page_size)
        for i in 0..<pageSize {
          labels.append(select_labels[i].map { String(cString: $0) } ?? "")
        }
      }
      // swiftlint:enable identifier_name
      let page = Int(ctx.menu.page_no)
      let lastPage = ctx.menu.is_last_page

      let selRange = NSRange(location: start.utf16Offset(in: preedit), length: preedit.utf16.distance(from: start, to: end))
      showPanel(preedit: inlinePreedit ? "" : preedit, selRange: selRange, caretPos: caretPos.utf16Offset(in: preedit),
                candidates: candidates, comments: comments, labels: labels, highlighted: Int(ctx.menu.highlighted_candidate_index),
                page: page, lastPage: lastPage)
      _ = rimeAPI.free_context(&ctx)
    } else {
      hidePalettes()
    }
  }

  func commit(string: String, establishMarkedText: Bool = false) {
    guard let client = client else { return }

    let forceMarkedText = establishMarkedText || (
      session != 0 &&
      rimeAPI.get_option(session, "force_marked_text_for_direct_commit"))

    // Direct commits such as full-width punctuation do not necessarily have an
    // active marked-text phase. Some NSTextInputClient implementations require
    // one before accepting insertText.
    if forceMarkedText && preedit.isEmpty && !string.isEmpty {
      let markedText = NSMutableAttributedString(string: string)
      client.setMarkedText(
        markedText,
        selectionRange: NSRange(location: markedText.length, length: 0),
        replacementRange: .empty
      )
    }

    client.insertText(string, replacementRange: .empty)
    preedit = ""
    caretPos = 0
    selRange = NSRange(location: 0, length: 0)
    hidePalettes()
  }

  func show(preedit: String, selRange: NSRange, caretPos: Int) {
    guard let client = client else { return }
    if self.preedit == preedit && self.caretPos == caretPos && self.selRange == selRange {
      return
    }

    self.preedit = preedit
    self.caretPos = caretPos
    self.selRange = selRange

    let start = selRange.location
    let attrString = NSMutableAttributedString(string: preedit)
    if start > 0 {
      let attrs = mark(forStyle: kTSMHiliteConvertedText, at: NSRange(location: 0, length: start))! as! [NSAttributedString.Key: Any]
      attrString.setAttributes(attrs, range: NSRange(location: 0, length: start))
    }
    let remainingRange = NSRange(location: start, length: preedit.utf16.count - start)
    let attrs = mark(forStyle: kTSMHiliteSelectedRawText, at: remainingRange)! as! [NSAttributedString.Key: Any]
    attrString.setAttributes(attrs, range: remainingRange)
    client.setMarkedText(attrString, selectionRange: NSRange(location: caretPos, length: 0), replacementRange: .empty)
  }

  // swiftlint:disable:next function_parameter_count
  func showPanel(preedit: String, selRange: NSRange, caretPos: Int, candidates: [String], comments: [String], labels: [String], highlighted: Int, page: Int, lastPage: Bool) {
    guard let client = client else { return }
    var inputPos = NSRect()
    client.attributes(forCharacterIndex: 0, lineHeightRectangle: &inputPos)
    if let panel = NSApp.squirrelAppDelegate.panel {
      panel.position = inputPos
      panel.inputController = self
      panel.update(preedit: preedit, selRange: selRange, caretPos: caretPos, candidates: candidates, comments: comments, labels: labels,
                   highlighted: highlighted, page: page, lastPage: lastPage, update: true)
    }
  }

  private func handleASCIIModeToggle(_ notification: Notification) {
    guard let enableASCII = notification.object as? Bool else { return }
    guard session != 0 && rimeAPI.find_session(session) else { return }

    rimeAPI.set_option(session, "ascii_mode", enableASCII)
    rimeUpdate()
  }

  private func reportASCIIMode(_: Notification) {
    guard client != nil else { return }
    guard session != 0 && rimeAPI.find_session(session) else { return }

    let isASCIIMode = rimeAPI.get_option(session, "ascii_mode")
    let status = isASCIIMode ? "ascii" : "nascii"

    DistributedNotificationCenter.default().postNotificationName(
      .init("VoiceFlyASCIIModeResponse"),
      object: status
    )
  }

}
