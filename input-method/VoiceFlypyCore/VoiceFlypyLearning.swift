import Foundation
import Darwin

enum VoiceFlypyVoiceTiming {
  static let armDelay: TimeInterval = 0.1
  static let holdMonitorInterval: TimeInterval = 0.05
  static let holdReleaseConfirmationSamples = 10
  static let serviceMicrophoneOpenTimeout: TimeInterval = 6.0
  static let clientStartFallbackTimeout: TimeInterval = 8.0
}

enum VoiceFlypyVoiceControlEvent {
  private static let leftControlKeyCode = 59
  private static let rightControlKeyCode = 62

  static func isExplicitRelease(
    keyCode: Int,
    controlFlagChanged: Bool,
    controlIsPresent: Bool
  ) -> Bool {
    guard keyCode == leftControlKeyCode || keyCode == rightControlKeyCode else {
      return false
    }
    return controlFlagChanged && !controlIsPresent
  }
}

struct VoiceFlypyVoiceHoldReleaseDetector {
  private(set) var consecutiveReleasedSamples = 0

  mutating func shouldStop(controlIsPhysicallyDown: Bool) -> Bool {
    if controlIsPhysicallyDown {
      consecutiveReleasedSamples = 0
      return false
    }
    consecutiveReleasedSamples += 1
    return consecutiveReleasedSamples >= VoiceFlypyVoiceTiming.holdReleaseConfirmationSamples
  }

  mutating func reset() {
    consecutiveReleasedSamples = 0
  }
}

struct VoiceFlypyVoicePartialPresentation: Equatable {
  let text: String
  let sequence: Int?
  let stableUTF16: Int

  var mutableRange: NSRange {
    NSRange(
      location: stableUTF16,
      length: text.utf16.count - stableUTF16
    )
  }
}

struct VoiceFlypyVoicePartialState {
  private(set) var lastSequence: Int?

  mutating func reset() {
    lastSequence = nil
  }

  mutating func accept(
    text: String,
    sequence: Int?,
    stableUTF16 requestedStableUTF16: Int?
  ) -> VoiceFlypyVoicePartialPresentation? {
    if let sequence = sequence,
       let lastSequence = lastSequence,
       sequence <= lastSequence {
      return nil
    }
    if let sequence = sequence {
      lastSequence = sequence
    }
    let stableUTF16 = Self.validUTF16Boundary(
      requestedStableUTF16 ?? 0,
      in: text
    )
    return VoiceFlypyVoicePartialPresentation(
      text: text,
      sequence: sequence,
      stableUTF16: stableUTF16
    )
  }

  private static func validUTF16Boundary(_ requested: Int, in text: String) -> Int {
    let utf16 = text.utf16
    var offset = min(max(0, requested), utf16.count)
    while offset > 0 {
      let utf16Index = utf16.index(utf16.startIndex, offsetBy: offset)
      if String.Index(utf16Index, within: text) != nil {
        return offset
      }
      offset -= 1
    }
    return 0
  }
}

struct VoiceFlypyRimeBuildDirectories: Equatable {
  let userData: URL
  let sharedData: URL
  let staging: URL

  init(userData: URL, sharedData: URL) {
    self.userData = userData.standardizedFileURL
    self.sharedData = sharedData.standardizedFileURL
    self.staging = userData
      .appendingPathComponent("build", isDirectory: true)
      .standardizedFileURL
  }
}

struct VoiceFlypyPhraseDraft: Equatable {
  let text: String
  let code: String
}

struct VoiceFlypyJournalGlyph: Equatable {
  let text: Character
  let code: String
}

enum VoiceFlypyFlypyCodeResolver {
  private static let consonants = Set("bpmfdtnlgkhjqxrzcswy")
  private static let finalKeys: [String: String] = [
    "iang": "l", "uang": "l", "iong": "s",
    "uan": "r", "uai": "k", "iao": "n", "ian": "m",
    "ing": "k", "eng": "g", "ang": "h", "ong": "s",
    "iu": "q", "ei": "w", "ue": "t", "ve": "t", "un": "y",
    "uo": "o", "ie": "p", "ai": "d", "en": "f", "an": "j",
    "ou": "z", "ia": "x", "ua": "x", "ao": "c", "ui": "v", "in": "b",
  ]

  static func canonicalCode(forPinyin source: String) -> String? {
    var syllable = source.lowercased()
      .replacingOccurrences(of: "u:", with: "v")
      .replacingOccurrences(of: "ü", with: "v")
    syllable.removeAll { !$0.isASCII || (!$0.isLetter && $0 != "v") }
    guard !syllable.isEmpty else { return nil }

    if syllable == "a" || syllable == "e" || syllable == "o" {
      return syllable + syllable
    }
    // Rime's Flypy algebra retains these ordinary two-key zero-initial forms.
    if ["ai", "an", "ao", "ei", "en", "er", "ou"].contains(syllable) {
      return syllable
    }
    if syllable == "ang" { return "ah" }
    if syllable == "eng" { return "eg" }
    if syllable == "ong" { return "os" }

    let initial: String
    let final: String
    if syllable.hasPrefix("zh") {
      initial = "v"
      final = String(syllable.dropFirst(2))
    } else if syllable.hasPrefix("ch") {
      initial = "i"
      final = String(syllable.dropFirst(2))
    } else if syllable.hasPrefix("sh") {
      initial = "u"
      final = String(syllable.dropFirst(2))
    } else if let first = syllable.first, consonants.contains(first) {
      initial = String(first)
      final = String(syllable.dropFirst())
    } else {
      return nil
    }

    if ["j", "q", "x", "y"].contains(initial), final == "u" {
      return initial + "v"
    }
    if final.count == 1, final.first?.isLetter == true {
      return initial + final
    }
    guard let finalKey = finalKeys[final] else { return nil }
    return initial + finalKey
  }

  static func resolve(
    text: String,
    inputHint: String?,
    readings: [Character: [String]]
  ) -> [String]? {
    let characters = Array(text)
    guard !characters.isEmpty else { return nil }
    let choices: [[String]] = characters.map { character in
      var seen = Set<String>()
      return (readings[character] ?? []).compactMap { reading in
        guard let code = canonicalCode(forPinyin: reading), seen.insert(code).inserted else {
          return nil
        }
        return code
      }
    }
    guard choices.allSatisfy({ !$0.isEmpty }) else { return nil }

    let hint = (inputHint ?? "").lowercased().filter { $0.isASCII && $0.isLetter }
    var bestCodes: [String]?
    var bestScore = Int.min
    var visited = 0

    func abbreviationScore(_ codes: [String]) -> Int? {
      guard !hint.isEmpty else { return 0 }
      let full = codes.joined()
      if full == hint { return 100_000 }

      var best: Int?
      func walk(_ index: Int, _ offset: String.Index, _ score: Int) {
        if index == codes.count {
          if offset == hint.endIndex { best = max(best ?? Int.min, score) }
          return
        }
        let code = codes[index]
        for length in 1...code.count {
          let fragment = String(code.prefix(length))
          guard hint[offset...].hasPrefix(fragment) else { continue }
          let next = hint.index(offset, offsetBy: fragment.count)
          let fullBonus = length == code.count ? 10 : 0
          walk(index + 1, next, score + length + fullBonus)
        }
      }
      walk(0, hint.startIndex, 0)
      if let best { return 10_000 + best }
      if full.hasPrefix(hint) { return 1_000 + hint.count }
      let initials = codes.compactMap(\.first).map(String.init).joined()
      if initials == hint { return 500 }
      return nil
    }

    func enumerate(_ index: Int, _ codes: [String], _ preference: Int) {
      guard visited < 4096 else { return }
      if index == choices.count {
        visited += 1
        guard let match = abbreviationScore(codes) else { return }
        let score = match - preference
        if score > bestScore {
          bestScore = score
          bestCodes = codes
        }
        return
      }
      for (choiceIndex, code) in choices[index].enumerated() {
        enumerate(index + 1, codes + [code], preference + choiceIndex)
      }
    }

    enumerate(0, [], 0)
    if bestCodes == nil, hint.isEmpty {
      bestCodes = choices.compactMap(\.first)
    }
    return bestCodes
  }
}

struct VoiceFlypyPinyinLexicon {
  private(set) var characterReadings: [Character: [String]] = [:]
  private(set) var phraseCodes: [String: [String]] = [:]

  init() {}

  init(entries: [(text: String, pinyin: String)]) {
    for entry in entries {
      add(text: entry.text, pinyin: entry.pinyin)
    }
  }

  mutating func add(text: String, pinyin: String) {
    let characters = Array(text)
    guard !characters.isEmpty else { return }
    if characters.count == 1 {
      guard !pinyin.isEmpty,
            !(characterReadings[characters[0]]?.contains(pinyin) ?? false) else { return }
      characterReadings[characters[0], default: []].append(pinyin)
      return
    }

    guard (2...5).contains(characters.count), phraseCodes[text] == nil else { return }
    let syllables = pinyin.split(whereSeparator: \.isWhitespace).map(String.init)
    guard syllables.count == characters.count else { return }
    let codes = syllables.compactMap(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin:))
    guard codes.count == characters.count else { return }
    phraseCodes[text] = codes
  }

  func codes(for text: String) -> [String]? {
    if let exact = phraseCodes[text] { return exact }
    return VoiceFlypyFlypyCodeResolver.resolve(
      text: text,
      inputHint: nil,
      readings: characterReadings
    )
  }

  func draft(
    fromVisiblePrefix visiblePrefix: String?,
    exactLength: Int? = nil,
    journalDraft: VoiceFlypyPhraseDraft? = nil
  ) -> VoiceFlypyPhraseDraft? {
    let text = exactLength.flatMap {
      VoiceFlypyVisiblePhraseExtractor.suffix(from: visiblePrefix, exactLength: $0)
    } ?? VoiceFlypyVisiblePhraseExtractor.suffix(from: visiblePrefix)
    guard let text else {
      return nil
    }
    // The host-visible suffix is the sole eligibility source. A matching
    // commit journal may only retain the input-time pronunciation choice for
    // polyphonic text; stale or unrelated history has no effect.
    if let journalDraft, journalDraft.text == text {
      return journalDraft
    }
    guard let codes = codes(for: text),
          codes.count == text.count else { return nil }
    return VoiceFlypyPhraseDraft(text: text, code: codes.joined())
  }
}

struct VoiceFlypyPhraseLearningJournal {
  private(set) var glyphs: [VoiceFlypyJournalGlyph] = []
  let capacity: Int

  init(capacity: Int = 5) {
    self.capacity = max(2, capacity)
  }

  @discardableResult
  mutating func recordCommitted(
    text: String,
    inputHint: String?,
    readings: [Character: [String]]
  ) -> String? {
    guard !text.isEmpty,
          !text.contains(where: { $0.isWhitespace }),
          let codes = VoiceFlypyFlypyCodeResolver.resolve(
            text: text,
            inputHint: inputHint,
            readings: readings
          ) else {
      reset()
      return nil
    }
    let characters = Array(text)
    guard characters.count == codes.count else {
      reset()
      return nil
    }
    glyphs.append(contentsOf: zip(characters, codes).map(VoiceFlypyJournalGlyph.init))
    glyphs = Array(glyphs.suffix(capacity))
    return codes.joined()
  }

  @discardableResult
  mutating func deleteBackward() -> Bool {
    guard !glyphs.isEmpty else { return false }
    glyphs.removeLast()
    return true
  }

  mutating func reset() {
    glyphs.removeAll(keepingCapacity: true)
  }

  var draft: VoiceFlypyPhraseDraft? {
    draft(length: glyphs.count)
  }

  func draft(length: Int) -> VoiceFlypyPhraseDraft? {
    guard (2...capacity).contains(length), glyphs.count >= length else { return nil }
    let selected = glyphs.suffix(length)
    return VoiceFlypyPhraseDraft(
      text: selected.map { String($0.text) }.joined(),
      code: selected.map(\.code).joined()
    )
  }
}

enum VoiceFlypySpaceGestureAction: Equatable {
  case insertSpaces(Int)
  case requestPhraseLength(fallbackSpaces: Int)
  case enterChaizi
}

struct VoiceFlypySpaceGesture {
  let timeout: TimeInterval
  private(set) var tapCount = 0
  private(set) var startedAt: TimeInterval?
  private var fallbackSpaceCount = 0

  init(timeout: TimeInterval = 0.55) {
    self.timeout = timeout
  }

  mutating func tap(
    at now: TimeInterval,
    fallbackSpaces: Int = 1
  ) -> [VoiceFlypySpaceGestureAction] {
    var actions: [VoiceFlypySpaceGestureAction] = []
    if let startedAt, now - startedAt > timeout {
      actions.append(contentsOf: settle())
    }
    if tapCount == 0 { startedAt = now }
    tapCount += 1
    fallbackSpaceCount += max(0, fallbackSpaces)
    if tapCount >= 3 {
      reset()
      actions.append(.enterChaizi)
    }
    return actions
  }

  mutating func settle() -> [VoiceFlypySpaceGestureAction] {
    defer { reset() }
    if tapCount == 2 {
      return [.requestPhraseLength(fallbackSpaces: fallbackSpaceCount)]
    }
    return fallbackSpaceCount > 0 ? [.insertSpaces(fallbackSpaceCount)] : []
  }

  mutating func cancel() {
    reset()
  }

  func remaining(at now: TimeInterval) -> TimeInterval? {
    guard tapCount > 0, let startedAt else { return nil }
    return max(0.01, timeout - (now - startedAt))
  }

  private mutating func reset() {
    tapCount = 0
    startedAt = nil
    fallbackSpaceCount = 0
  }
}

struct VoiceFlypyRecentCommit {
  let lifetime: TimeInterval
  private var draft: VoiceFlypyPhraseDraft?
  private var expiresAt: TimeInterval?

  init(lifetime: TimeInterval = 1.2) {
    self.lifetime = max(0, lifetime)
  }

  mutating func record(_ draft: VoiceFlypyPhraseDraft, at now: TimeInterval) {
    self.draft = draft
    expiresAt = now + lifetime
  }

  mutating func invalidate() {
    draft = nil
    expiresAt = nil
  }

  func validDraft(at now: TimeInterval) -> VoiceFlypyPhraseDraft? {
    guard let draft, let expiresAt, now <= expiresAt else { return nil }
    return draft
  }
}

enum VoiceFlypyChaiziBackspaceAction: Equatable {
  case deleteRawInput
  case returnToComponents
  case removeLastComponent
  case exit
}

enum VoiceFlypyChaiziInteractionRules {
  static func isPhysicalBackspace(keyCode: Int, character: Character?) -> Bool {
    keyCode == 51 || character == "\u{8}" || character == "\u{7f}"
  }

  static func backspaceAction(
    hasRawInput: Bool,
    componentCount: Int,
    hasResults: Bool
  ) -> VoiceFlypyChaiziBackspaceAction {
    if hasRawInput { return .deleteRawInput }
    if hasResults { return .returnToComponents }
    if componentCount > 0 { return .removeLastComponent }
    return .exit
  }

  static func shouldRestoreMarker(isActive: Bool, hasRawInput: Bool) -> Bool {
    isActive && !hasRawInput
  }

  static func isTildeExit(_ character: Character?) -> Bool {
    character == "~"
  }
}

enum VoiceFlypyVisiblePhraseExtractor {
  private static let trailingBoundaries = Set(
    "，。！？；：、,.!?;:…“”‘’\"'（）()【】[]《》<>"
  )
  private static func isCJKUnifiedIdeograph(_ value: UInt32) -> Bool {
    value == 0x3007 ||
      (0x3400...0x4DBF).contains(value) ||
      (0x4E00...0x9FFF).contains(value) ||
      (0xF900...0xFAFF).contains(value) ||
      (0x20000...0x2A6DF).contains(value) ||
      (0x2A700...0x2B73F).contains(value) ||
      (0x2B740...0x2B81F).contains(value) ||
      (0x2B820...0x2CEAF).contains(value) ||
      (0x2CEB0...0x2EBEF).contains(value) ||
      (0x2EBF0...0x2EE5F).contains(value) ||
      (0x2F800...0x2FA1F).contains(value) ||
      (0x30000...0x3134F).contains(value) ||
      (0x31350...0x323AF).contains(value)
  }

  static func isCJK(_ character: Character) -> Bool {
    guard let base = character.unicodeScalars.first else { return false }
    return isCJKUnifiedIdeograph(base.value)
  }

  private static func isTrailingBoundary(_ character: Character) -> Bool {
    character.isWhitespace || trailingBoundaries.contains(character)
  }

  /// Extract the Chinese run at the caret after harmless trailing sentence
  /// boundaries. Older text, input history, paste/undo operations, and
  /// punctuation before that run are intentionally irrelevant. The newest
  /// five glyphs win for longer runs. Explicit learning uses the exact-length
  /// overload below; this default exists for bounded recent-commit fallback.
  static func suffix(
    from visiblePrefix: String?,
    minimumLength: Int = 2,
    maximumLength: Int = 5
  ) -> String? {
    guard let visiblePrefix,
          minimumLength > 0,
          maximumLength >= minimumLength else { return nil }
    // Voice dictation and direct commits can legitimately leave sentence
    // punctuation or a separator immediately after the intended phrase. The
    // double-space gesture is explicit, so ignore only trailing boundaries;
    // letters, digits, emoji and other symbols still fail closed.
    let contentFromEnd = visiblePrefix.reversed().drop(while: isTrailingBoundary)
    let contiguous = contentFromEnd.prefix { isCJK($0) }.reversed()
    guard contiguous.count >= minimumLength else { return nil }
    return String(contiguous.suffix(maximumLength))
  }

  /// Return exactly the number of caret-adjacent Han glyphs explicitly chosen
  /// by the user. This avoids guessing a word boundary inside a longer Chinese
  /// run (for example, choosing `紧凶` rather than `围偏紧凶`).
  static func suffix(from visiblePrefix: String?, exactLength: Int) -> String? {
    guard let visiblePrefix, (2...5).contains(exactLength) else { return nil }
    let contentFromEnd = visiblePrefix.reversed().drop(while: isTrailingBoundary)
    let contiguous = contentFromEnd.prefix { isCJK($0) }.reversed()
    guard contiguous.count >= exactLength else { return nil }
    return String(contiguous.suffix(exactLength))
  }

  static func matchesVisibleSuffix(text: String, visiblePrefix: String?) -> Bool {
    suffix(from: visiblePrefix, exactLength: text.count) == text
  }

}

enum VoiceFlypyLearnedPhraseDeploymentRules {
  static func isSafe(
    hasValidSession: Bool,
    voiceArmed: Bool,
    voiceActive: Bool,
    chaiziMode: Bool,
    pendingSpaceTapCount: Int,
    hasRawInput: Bool
  ) -> Bool {
    hasValidSession &&
      !voiceArmed &&
      !voiceActive &&
      !chaiziMode &&
      pendingSpaceTapCount == 0 &&
      !hasRawInput
  }
}

/// Immutable, query-oriented view of the IDS source.  The old implementation
/// recursively flattened and sorted every one of the ~88k definitions on every
/// Return/three-space query.  Build those representations once off the typing
/// path and use exact-key maps plus the rarest component posting list instead.
struct VoiceFlypyChaiziIndex {
  private struct Entry {
    let text: String
    let leafCount: Int
    let leafCounts: [Character: Int]
  }

  private let flattenedByCharacter: [Character: [Character]]
  private let entries: [Entry]
  private let orderedDirect: [String: [Int]]
  private let unorderedDirect: [String: [Int]]
  private let unorderedLeaves: [String: [Int]]
  private let leafPostings: [Character: [Int]]

  init(definitions: [Character: [Character]]) {
    var flattenedByCharacter: [Character: [Character]] = [:]
    flattenedByCharacter.reserveCapacity(definitions.count)
    for character in definitions.keys {
      flattenedByCharacter[character] = Self.flattened(
        character,
        definitions: definitions,
        depth: 0,
        seen: []
      )
    }

    var entries: [Entry] = []
    var orderedDirect: [String: [Int]] = [:]
    var unorderedDirect: [String: [Int]] = [:]
    var unorderedLeaves: [String: [Int]] = [:]
    var leafPostings: [Character: [Int]] = [:]
    entries.reserveCapacity(definitions.count)

    // Stable source ordering makes equal-score lookup deterministic across
    // launches regardless of Dictionary iteration order.
    for target in definitions.keys.sorted(by: { String($0) < String($1) }) {
      guard let parts = definitions[target] else { continue }
      let direct = parts.map(Self.canonical)
      let leaves = flattenedByCharacter[target] ?? [Self.canonical(target)]
      var counts: [Character: Int] = [:]
      for leaf in leaves { counts[leaf, default: 0] += 1 }

      let index = entries.count
      entries.append(Entry(text: String(target), leafCount: leaves.count, leafCounts: counts))
      orderedDirect[Self.key(direct), default: []].append(index)
      unorderedDirect[Self.key(Self.sorted(direct)), default: []].append(index)
      unorderedLeaves[Self.key(Self.sorted(leaves)), default: []].append(index)
      for leaf in counts.keys {
        leafPostings[leaf, default: []].append(index)
      }
    }

    self.flattenedByCharacter = flattenedByCharacter
    self.entries = entries
    self.orderedDirect = orderedDirect
    self.unorderedDirect = unorderedDirect
    self.unorderedLeaves = unorderedLeaves
    self.leafPostings = leafPostings
  }

  func lookup(components: [String], limit: Int = 25) -> [String] {
    let componentCharacters = components.compactMap(\.first)
    guard !componentCharacters.isEmpty, limit > 0 else { return [] }
    let queryDirect = componentCharacters.map(Self.canonical)
    let queryLeaves = componentCharacters.flatMap {
      flattenedByCharacter[$0] ?? [Self.canonical($0)]
    }
    guard !queryLeaves.isEmpty else { return [] }

    var bestScores: [Int: Int] = [:]
    func consider(_ indices: [Int]?, score: Int) {
      guard let indices else { return }
      for index in indices {
        bestScores[index] = min(bestScores[index] ?? Int.max, score)
      }
    }

    consider(orderedDirect[Self.key(queryDirect)], score: 0)
    consider(unorderedDirect[Self.key(Self.sorted(queryDirect))], score: 1)
    consider(unorderedLeaves[Self.key(Self.sorted(queryLeaves))], score: 2)

    var requiredCounts: [Character: Int] = [:]
    for leaf in queryLeaves { requiredCounts[leaf, default: 0] += 1 }
    if let seed = requiredCounts.keys.min(by: {
      (leafPostings[$0]?.count ?? Int.max) < (leafPostings[$1]?.count ?? Int.max)
    }), let candidates = leafPostings[seed] {
      for index in candidates {
        let entry = entries[index]
        guard requiredCounts.allSatisfy({ entry.leafCounts[$0.key, default: 0] >= $0.value }) else {
          continue
        }
        let score = 10 + entry.leafCount - queryLeaves.count
        bestScores[index] = min(bestScores[index] ?? Int.max, score)
      }
    }

    var scored = bestScores.map { item in
      (score: item.value, text: entries[item.key].text)
    }
    scored.sort {
      $0.score == $1.score ? $0.text < $1.text : $0.score < $1.score
    }
    return Array(scored.prefix(limit)).map(\.text)
  }

  private static func canonical(_ char: Character) -> Character {
    let variants: [Character: Character] = [
      "亻": "人", "⺅": "人", "氵": "水", "⺡": "水", "扌": "手",
      "忄": "心", "讠": "言", "钅": "金", "饣": "食", "覀": "西",
      "朩": "木", "礻": "示", "衤": "衣", "灬": "火", "犭": "犬",
      "刂": "刀",
    ]
    return variants[char] ?? char
  }

  private static func flattened(
    _ char: Character,
    definitions: [Character: [Character]],
    depth: Int,
    seen: Set<Character>
  ) -> [Character] {
    let value = canonical(char)
    guard depth < 8,
          !seen.contains(char),
          let parts = definitions[char],
          !(parts.count == 1 && parts[0] == char) else { return [value] }
    return parts.flatMap {
      flattened($0, definitions: definitions, depth: depth + 1, seen: seen.union([char]))
    }
  }

  private static func sorted(_ characters: [Character]) -> [Character] {
    characters.sorted(by: { String($0) < String($1) })
  }

  private static func key(_ characters: [Character]) -> String {
    characters.map(String.init).joined(separator: "\u{0}")
  }
}

enum VoiceFlypyLearningStoreError: LocalizedError {
  case invalidExistingFile(String)
  case unsafeSymlink(URL)
  case coordination(String)

  var errorDescription: String? {
    switch self {
    case let .invalidExistingFile(detail): return detail
    case let .unsafeSymlink(url): return "refusing symlink: \(url.path)"
    case let .coordination(detail): return detail
    }
  }
}

private enum VoiceFlypyFileLock {
  static func withLock<T>(
    for target: URL,
    timeout: TimeInterval,
    retryInterval: TimeInterval = 0.01,
    _ body: () throws -> T
  ) throws -> T {
    let lockURL = target.deletingLastPathComponent()
      .appendingPathComponent(".\(target.lastPathComponent).lock")
    let descriptor = lockURL.path.withCString {
      Darwin.open($0, O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, S_IRUSR | S_IWUSR)
    }
    guard descriptor >= 0 else {
      throw VoiceFlypyLearningStoreError.coordination(
        "cannot open learning-store lock: \(String(cString: strerror(errno)))"
      )
    }
    defer { Darwin.close(descriptor) }

    let deadline = ProcessInfo.processInfo.systemUptime + max(0, timeout)
    while flock(descriptor, LOCK_EX | LOCK_NB) != 0 {
      let lockError = errno
      if lockError != EWOULDBLOCK && lockError != EAGAIN && lockError != EINTR {
        throw VoiceFlypyLearningStoreError.coordination(
          "cannot lock learning store: \(String(cString: strerror(lockError)))"
        )
      }
      let remaining = deadline - ProcessInfo.processInfo.systemUptime
      guard remaining > 0 else {
        throw VoiceFlypyLearningStoreError.coordination(
          "timed out waiting for learning-store lock: \(lockURL.path)"
        )
      }
      Thread.sleep(forTimeInterval: min(max(0.001, retryInterval), remaining))
    }
    defer { flock(descriptor, LOCK_UN) }
    return try body()
  }
}

final class VoiceFlypyLearnedPhraseStore {
  static let changedNotification = Notification.Name("com.simonli.voiceflypy.lexicon-changed")

  private let url: URL
  private let notifier: (_ generation: String, _ url: URL) -> Void
  private let lockTimeout: TimeInterval

  init(
    url: URL,
    lockTimeout: TimeInterval = 1,
    notifier: @escaping (_ generation: String, _ url: URL) -> Void = { generation, url in
      DistributedNotificationCenter.default().postNotificationName(
        changedNotification,
        object: nil,
        userInfo: ["generation": generation, "path": url.path],
        deliverImmediately: true
      )
    }
  ) {
    self.url = url
    self.lockTimeout = lockTimeout
    self.notifier = notifier
  }

  @discardableResult
  func save(phrase: String, code: String) throws -> Bool {
    guard (2...5).contains(phrase.count),
          !phrase.contains(where: { $0.isWhitespace }),
          !code.isEmpty,
          code.allSatisfy({ $0.isASCII && $0.isLetter }) else {
      throw VoiceFlypyLearningStoreError.invalidExistingFile("invalid learned phrase or code")
    }
    try rejectSymlink(url)
    try FileManager.default.createDirectory(
      at: url.deletingLastPathComponent(),
      withIntermediateDirectories: true
    )

    let changed = try VoiceFlypyFileLock.withLock(for: url, timeout: lockTimeout) {
        try self.rejectSymlink(self.url)
        var content: String
        if FileManager.default.fileExists(atPath: self.url.path) {
          content = try String(contentsOf: self.url, encoding: .utf8)
          try self.validate(content)
        } else {
          content = "# VoiceFlypy user-learned phrases. Never overwritten by profile publication.\n"
        }
        let exists = content.split(whereSeparator: \.isNewline).contains { line in
          let fields = line.split(separator: "\t", omittingEmptySubsequences: false)
          return fields.count >= 2 && fields[0] == phrase && fields[1] == code
        }
        guard !exists else { return false }
        if !content.hasSuffix("\n") { content.append("\n") }
        content.append("\(phrase)\t\(code)\t200\n")
        try self.validate(content)
        try content.write(to: self.url, atomically: true, encoding: .utf8)
        return true
    }
    if changed { notifier(UUID().uuidString, url) }
    return changed
  }

  private func validate(_ content: String) throws {
    for (index, line) in content.split(whereSeparator: \.isNewline).enumerated() {
      if line.isEmpty || line.hasPrefix("#") { continue }
      let fields = line.split(separator: "\t", omittingEmptySubsequences: false)
      guard (2...3).contains(fields.count),
            (1...12).contains(fields[0].count),
            !fields[1].isEmpty,
            fields[1].allSatisfy({ $0.isASCII && $0.isLetter }),
            fields.count < 3 || Int(fields[2]) != nil else {
        throw VoiceFlypyLearningStoreError.invalidExistingFile(
          "invalid learned phrase file at line \(index + 1); original preserved"
        )
      }
    }
  }

  private func rejectSymlink(_ candidate: URL) throws {
    let values = try? candidate.resourceValues(forKeys: [.isSymbolicLinkKey])
    if values?.isSymbolicLink == true {
      throw VoiceFlypyLearningStoreError.unsafeSymlink(candidate)
    }
  }
}

final class VoiceFlypyPersonalUsageStore {
  struct Term: Codable, Equatable {
    var flypy_code: String
    var selection_count: Int
    var explicit_count: Int
    var updated_at: String
  }

  struct Document: Codable, Equatable {
    var schema_version = "1.0.0"
    var terms: [String: Term] = [:]
  }

  private struct Delta {
    var code: String
    var selections: Int
    var explicits: Int
    var updatedAt: String
  }

  private let url: URL
  private let mirrorURL: URL?
  private let queue: DispatchQueue
  private let notifier: () -> Void
  private let lockTimeout: TimeInterval
  private var pending: [String: Delta] = [:]
  private var flushWorkItem: DispatchWorkItem?

  init(
    url: URL,
    mirrorURL: URL? = nil,
    lockTimeout: TimeInterval = 0.25,
    notifier: @escaping () -> Void = {
      DistributedNotificationCenter.default().postNotificationName(
        VoiceFlypyLearnedPhraseStore.changedNotification,
        object: nil,
        userInfo: ["generation": UUID().uuidString],
        deliverImmediately: true
      )
    }
  ) {
    self.url = url
    self.mirrorURL = mirrorURL
    self.lockTimeout = lockTimeout
    self.notifier = notifier
    queue = DispatchQueue(label: "com.simonli.voiceflypy.personal-usage")
  }

  func record(text: String, code: String, explicit: Bool) {
    let compact = text.filter { !$0.isWhitespace }
    guard (2...12).contains(compact.count),
          !code.isEmpty,
          code.allSatisfy({ $0.isASCII && $0.isLetter }) else { return }
    queue.async {
      let timestamp = ISO8601DateFormatter().string(from: Date())
      var delta = self.pending[compact] ?? Delta(
        code: code,
        selections: 0,
        explicits: 0,
        updatedAt: timestamp
      )
      delta.code = code
      delta.selections += explicit ? 0 : 1
      delta.explicits += explicit ? 1 : 0
      delta.updatedAt = timestamp
      self.pending[compact] = delta
      self.scheduleFlushLocked(after: explicit ? 0.05 : 1.0)
    }
  }

  @discardableResult
  func flushSynchronously(timeout: TimeInterval = 0.5) -> Bool {
    final class ResultBox {
      var value = false
    }
    let result = ResultBox()
    let completed = DispatchSemaphore(value: 0)
    queue.async {
      self.flushWorkItem?.cancel()
      self.flushWorkItem = nil
      result.value = self.flushLocked(scheduleRetry: false)
      completed.signal()
    }
    guard completed.wait(timeout: .now() + max(0, timeout)) == .success else { return false }
    return result.value
  }

  /// Publish a validated, non-iCloud runtime snapshot without changing the
  /// synced ledger's ownership. Voice recognition reads this mirror so a
  /// dataless iCloud placeholder cannot block hotword refresh.
  @discardableResult
  func refreshMirrorSynchronously(timeout: TimeInterval = 1) -> Bool {
    guard mirrorURL != nil else { return true }
    final class ResultBox {
      var value = false
      var changed = false
    }
    let result = ResultBox()
    let completed = DispatchSemaphore(value: 0)
    queue.async {
      do {
        // A temporarily unavailable iCloud container can present as a missing
        // primary path.  Once a validated local generation exists, absence is
        // not authority to replace it with an empty document.
        if !FileManager.default.fileExists(atPath: self.url.path),
           let mirrorURL = self.mirrorURL,
           FileManager.default.fileExists(atPath: mirrorURL.path) {
          result.value = true
          completed.signal()
          return
        }
        let document = try self.loadDocument(self.url)
        try self.validate(document)
        result.changed = try self.publishMirror(self.encoded(document))
        result.value = true
      } catch {
        print("Unable to refresh local personal-usage snapshot: \(error)")
      }
      completed.signal()
    }
    guard completed.wait(timeout: .now() + max(0, timeout)) == .success else { return false }
    if result.changed { notifier() }
    return result.value
  }

  private func scheduleFlushLocked(after delay: TimeInterval) {
    flushWorkItem?.cancel()
    let item = DispatchWorkItem { [weak self] in
      guard let self else { return }
      self.flushWorkItem = nil
      _ = self.flushLocked(scheduleRetry: true)
    }
    flushWorkItem = item
    queue.asyncAfter(deadline: .now() + delay, execute: item)
  }

  private func flushLocked(scheduleRetry: Bool) -> Bool {
    guard !pending.isEmpty else { return true }
    let snapshot = pending
    var runtimeData: Data?
    do {
      try rejectSymlink(url)
      try FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
      )
      try VoiceFlypyFileLock.withLock(for: url, timeout: lockTimeout) {
          try self.rejectSymlink(self.url)
          var document = try self.loadDocument(self.url)
          for (text, delta) in snapshot {
            var term = document.terms[text] ?? Term(
              flypy_code: delta.code,
              selection_count: 0,
              explicit_count: 0,
              updated_at: delta.updatedAt
            )
            term.flypy_code = delta.code
            let selection = term.selection_count.addingReportingOverflow(delta.selections)
            let explicit = term.explicit_count.addingReportingOverflow(delta.explicits)
            guard !selection.overflow, !explicit.overflow else {
              throw VoiceFlypyLearningStoreError.invalidExistingFile(
                "personal usage counter overflow for \(text); original preserved"
              )
            }
            term.selection_count = selection.partialValue
            term.explicit_count = explicit.partialValue
            term.updated_at = delta.updatedAt
            document.terms[text] = term
          }
          try self.validate(document)
          let data = try self.encoded(document)
          try data.write(to: self.url, options: .atomic)
          runtimeData = data
      }
      for key in snapshot.keys { pending.removeValue(forKey: key) }
      if let runtimeData {
        do {
          _ = try publishMirror(runtimeData)
        } catch {
          // The synchronized primary commit has already succeeded. Never keep
          // the delta pending and count it twice merely because a derived local
          // snapshot failed; the next write/preload retries the mirror.
          print("Unable to publish local personal-usage snapshot: \(error)")
        }
      }
      notifier()
      return true
    } catch {
      print("Unable to persist personal usage; original preserved: \(error)")
      if scheduleRetry { scheduleFlushLocked(after: 5) }
      return false
    }
  }

  private func loadDocument(_ candidate: URL) throws -> Document {
    guard FileManager.default.fileExists(atPath: candidate.path) else { return Document() }
    let data = try Data(contentsOf: candidate)
    let document: Document
    do {
      document = try JSONDecoder().decode(Document.self, from: data)
    } catch {
      throw VoiceFlypyLearningStoreError.invalidExistingFile(
        "personal usage JSON is invalid; original preserved: \(error.localizedDescription)"
      )
    }
    try validate(document)
    return document
  }

  private func encoded(_ document: Document) throws -> Data {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    return try encoder.encode(document)
  }

  @discardableResult
  private func publishMirror(_ data: Data) throws -> Bool {
    guard let mirrorURL else { return false }
    try rejectSymlink(mirrorURL)
    try FileManager.default.createDirectory(
      at: mirrorURL.deletingLastPathComponent(),
      withIntermediateDirectories: true
    )
    if (try? Data(contentsOf: mirrorURL)) == data { return false }
    try data.write(to: mirrorURL, options: .atomic)
    return true
  }

  private func validate(_ document: Document) throws {
    guard document.schema_version == "1.0.0" else {
      throw VoiceFlypyLearningStoreError.invalidExistingFile(
        "unsupported personal usage schema \(document.schema_version); original preserved"
      )
    }
    for (text, term) in document.terms {
      guard (2...12).contains(text.count),
            !text.contains(where: { $0.isWhitespace }),
            !term.flypy_code.isEmpty,
            term.flypy_code.allSatisfy({ $0.isASCII && $0.isLetter }),
            term.selection_count >= 0,
            term.explicit_count >= 0 else {
        throw VoiceFlypyLearningStoreError.invalidExistingFile(
          "invalid personal usage entry for \(text); original preserved"
        )
      }
    }
  }

  private func rejectSymlink(_ candidate: URL) throws {
    let values = try? candidate.resourceValues(forKeys: [.isSymbolicLinkKey])
    if values?.isSymbolicLink == true {
      throw VoiceFlypyLearningStoreError.unsafeSymlink(candidate)
    }
  }
}
