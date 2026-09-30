import Foundation
import XCTest
@testable import VoiceFlyCore

final class VoiceFlySettingsTests: XCTestCase {
  private func isolated(_ body: (VoiceFlyPaths) throws -> Void) throws {
    let home = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: home) }
    try body(VoiceFlyPaths(home: home))
  }
  private var resources: URL {
    URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
      .deletingLastPathComponent().appendingPathComponent("data/voicefly")
  }
  func testFreshInstallationDoesNotRequireICloud() throws {
    try isolated { paths in
      let store = VoiceFlySettingsStore(paths: paths)
      XCTAssertEqual(try store.load().schema, .pinyin)
      try store.save(VoiceFlySettings())
      XCTAssertTrue(FileManager.default.fileExists(atPath: paths.settings.path))
      XCTAssertFalse(FileManager.default.fileExists(atPath: paths.legacyWords.path))
    }
  }
  func testExistingFlypyPreferenceAndCustomConfigurationSurvive() throws {
    try isolated { paths in
      try FileManager.default.createDirectory(at: paths.rime, withIntermediateDirectories: true)
      try "patch:\n".write(to: paths.rime.appendingPathComponent("double_pinyin_flypy.custom.yaml"), atomically: true, encoding: .utf8)
      let existing = "patch:\n  schema_list:\n    - schema: double_pinyin_flypy\n  switcher/hotkeys: [Control+grave]\n"
      let custom = paths.rime.appendingPathComponent("default.custom.yaml")
      try existing.write(to: custom, atomically: true, encoding: .utf8)
      let store = VoiceFlySettingsStore(paths: paths)
      XCTAssertEqual(try store.load().schema, .flypy)
      try store.prepareRime(try store.load())
      try store.prepareRime(try store.load())
      let result = try String(contentsOf: custom)
      XCTAssertTrue(result.contains("switcher/hotkeys: [Control+grave]"))
      XCTAssertTrue(result.contains("- schema: double_pinyin_flypy"))
      XCTAssertEqual(result.components(separatedBy: "# Voice Fly managed schemes").count, 2)
      XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: paths.backups.path).count, 1)
    }
  }
  func testInvalidSettingsDoNotOverwriteLastValidFile() throws {
    try isolated { paths in
      let store = VoiceFlySettingsStore(paths: paths)
      try store.save(VoiceFlySettings())
      let before = try Data(contentsOf: paths.settings)
      var bad = VoiceFlySettings(); bad.voiceKey = "command"; bad.candidateCount = 100
      XCTAssertThrowsError(try store.save(bad))
      XCTAssertEqual(try Data(contentsOf: paths.settings), before)
    }
  }
  func testSharedWordGeneratesAllKeyboardCodesAndEditablePronunciation() throws {
    try isolated { paths in
      let manager = VoiceFlyWordManager(paths: paths, resources: resources)
      try manager.put(text: "人工智能", pinyin: "ren gong zhi neng")
      let words = try manager.list()
      XCTAssertEqual(words.count, 1); XCTAssertEqual(words[0].pinyin, "ren gong zhi neng")
      for schema in VoiceFlySchema.allCases {
        let text = try String(contentsOf: paths.rime.appendingPathComponent("voicefly_words_\(schema.rawValue).txt"))
        XCTAssertTrue(text.contains("人工智能\t"), schema.rawValue)
      }
      let full = try String(contentsOf: paths.rime.appendingPathComponent("voicefly_words_pinyin.txt"))
      let flypy = try String(contentsOf: paths.rime.appendingPathComponent("voicefly_words_flypy.txt"))
      XCTAssertTrue(full.contains("rengongzhineng")); XCTAssertTrue(flypy.contains("rfgsving"))
      try manager.put(text: "银行", pinyin: "yin hang")
      XCTAssertEqual(try manager.list().first(where: { $0.text == "银行" })?.pinyin, "yin hang")
    }
  }
  func testDeletionRemovesSharedLedgerAndOldFlypyOverlay() throws {
    try isolated { paths in
      let manager = VoiceFlyWordManager(paths: paths, resources: resources)
      try manager.put(text: "人工智能", pinyin: "ren gong zhi neng")
      let legacy = paths.rime.appendingPathComponent("voicefly_learned_phrases.txt")
      try "# preserved header\n人工智能\trfgsving\t200\n语音\tuyyb\t200\n".write(to: legacy, atomically: true, encoding: .utf8)
      try manager.remove(text: "人工智能")
      XCTAssertEqual(try manager.list().count, 0)
      let old = try String(contentsOf: legacy)
      XCTAssertFalse(old.contains("人工智能")); XCTAssertTrue(old.contains("语音"))
      for schema in VoiceFlySchema.allCases {
        XCTAssertFalse(try String(contentsOf: paths.rime.appendingPathComponent("voicefly_words_\(schema.rawValue).txt")).contains("人工智能"))
      }
    }
  }
  func testInvalidImportIsRejectedBeforeAnyCommit() throws {
    try isolated { paths in
      let manager = VoiceFlyWordManager(paths: paths, resources: resources)
      try manager.put(text: "人工智能", pinyin: "ren gong zhi neng")
      let before = try Data(contentsOf: paths.words)
      let invalid = paths.home.appendingPathComponent("bad.json")
      try #"{"schema_version":"1.0.0","terms":{"坏词":{"flypy_code":"../../x","selection_count":0,"explicit_count":1,"updated_at":""}}}"#.write(to: invalid, atomically: true, encoding: .utf8)
      XCTAssertThrowsError(try manager.importWords(from: invalid))
      XCTAssertEqual(try Data(contentsOf: paths.words), before)
    }
  }
  func testMalformedLocalLedgerNeverGetsReplacedByCloudCopy() throws {
    try isolated { paths in
      try FileManager.default.createDirectory(at: paths.words.deletingLastPathComponent(), withIntermediateDirectories: true)
      try "invalid local".write(to: paths.words, atomically: true, encoding: .utf8)
      let manager = VoiceFlyWordManager(paths: paths, resources: resources)
      try manager.migrateLegacyIfNeeded()
      XCTAssertThrowsError(try manager.put(text: "人工智能", pinyin: "ren gong zhi neng"))
      XCTAssertEqual(try String(contentsOf: paths.words), "invalid local")
    }
  }
  func testSymlinkSettingsAreNeverOverwritten() throws {
    try isolated { paths in
      try FileManager.default.createDirectory(at: paths.support, withIntermediateDirectories: true)
      let destination = paths.home.appendingPathComponent("protected")
      try "keep".write(to: destination, atomically: true, encoding: .utf8)
      try FileManager.default.createSymbolicLink(at: paths.settings, withDestinationURL: destination)
      XCTAssertThrowsError(try VoiceFlySettingsStore(paths: paths).save(VoiceFlySettings()))
      XCTAssertEqual(try String(contentsOf: destination), "keep")
    }
  }
}
