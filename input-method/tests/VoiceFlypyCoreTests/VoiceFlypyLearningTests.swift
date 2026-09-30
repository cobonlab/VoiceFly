import Foundation
import Darwin
import XCTest
@testable import VoiceFlypyCore

final class VoiceTimingTests: XCTestCase {
  func testClientFallbackOutlivesServiceMicrophoneWatchdog() {
    XCTAssertEqual(VoiceFlypyVoiceTiming.armDelay, 0.1)
    XCTAssertEqual(VoiceFlypyVoiceTiming.holdMonitorInterval, 0.05)
    XCTAssertEqual(VoiceFlypyVoiceTiming.holdReleaseConfirmationSamples, 10)
    XCTAssertEqual(VoiceFlypyVoiceTiming.serviceMicrophoneOpenTimeout, 6.0)
    XCTAssertEqual(VoiceFlypyVoiceTiming.clientStartFallbackTimeout, 8.0)
    XCTAssertGreaterThan(
      VoiceFlypyVoiceTiming.clientStartFallbackTimeout,
      VoiceFlypyVoiceTiming.serviceMicrophoneOpenTimeout
    )
    XCTAssertGreaterThanOrEqual(
      VoiceFlypyVoiceTiming.clientStartFallbackTimeout
        - VoiceFlypyVoiceTiming.serviceMicrophoneOpenTimeout,
      2.0
    )
  }

  func testHoldReleaseRequiresConsecutivePhysicalKeyUpSamples() {
    var detector = VoiceFlypyVoiceHoldReleaseDetector()

    XCTAssertFalse(detector.shouldStop(controlIsPhysicallyDown: false))
    XCTAssertFalse(detector.shouldStop(controlIsPhysicallyDown: true))
    XCTAssertEqual(detector.consecutiveReleasedSamples, 0)
    for _ in 1..<VoiceFlypyVoiceTiming.holdReleaseConfirmationSamples {
      XCTAssertFalse(detector.shouldStop(controlIsPhysicallyDown: false))
    }
    XCTAssertTrue(detector.shouldStop(controlIsPhysicallyDown: false))
  }

  func testOnlyARealControlKeyEventIsAnImmediateRelease() {
    XCTAssertTrue(VoiceFlypyVoiceControlEvent.isExplicitRelease(
      keyCode: 59,
      controlFlagChanged: true,
      controlIsPresent: false
    ))
    XCTAssertTrue(VoiceFlypyVoiceControlEvent.isExplicitRelease(
      keyCode: 62,
      controlFlagChanged: true,
      controlIsPresent: false
    ))
    XCTAssertFalse(VoiceFlypyVoiceControlEvent.isExplicitRelease(
      keyCode: 56,
      controlFlagChanged: true,
      controlIsPresent: false
    ))
    XCTAssertFalse(VoiceFlypyVoiceControlEvent.isExplicitRelease(
      keyCode: 59,
      controlFlagChanged: false,
      controlIsPresent: false
    ))
  }
}

final class VoicePartialStateTests: XCTestCase {
  func testAcceptsMonotonicUpdatesAndMarksOnlyMutableTail() {
    var state = VoiceFlypyVoicePartialState()
    let first = state.accept(text: "现在有三个问题", sequence: 1, stableUTF16: 3)
    let second = state.accept(text: "现在有三个问题", sequence: 2, stableUTF16: 5)

    XCTAssertEqual(first?.mutableRange, NSRange(location: 3, length: 4))
    XCTAssertEqual(second?.mutableRange, NSRange(location: 5, length: 2))
    XCTAssertEqual(state.lastSequence, 2)
  }

  func testRejectsDuplicateAndOutOfOrderNotifications() {
    var state = VoiceFlypyVoicePartialState()
    XCTAssertNotNil(state.accept(text: "甲", sequence: 4, stableUTF16: 0))
    XCTAssertNil(state.accept(text: "旧", sequence: 3, stableUTF16: 0))
    XCTAssertNil(state.accept(text: "重", sequence: 4, stableUTF16: 0))
  }

  func testClampsStableOffsetToAValidUTF16Boundary() {
    var state = VoiceFlypyVoicePartialState()
    let insideEmoji = state.accept(text: "A😀中", sequence: 1, stableUTF16: 2)
    let beyondEnd = state.accept(text: "A😀中", sequence: 2, stableUTF16: 99)

    XCTAssertEqual(insideEmoji?.stableUTF16, 1)
    XCTAssertEqual(beyondEnd?.stableUTF16, 4)
  }

  func testLegacyNotificationWithoutMetadataRemainsCompatible() {
    var state = VoiceFlypyVoicePartialState()
    let presentation = state.accept(text: "旧服务", sequence: nil, stableUTF16: nil)
    XCTAssertEqual(presentation?.mutableRange, NSRange(location: 0, length: 3))
  }
}

final class RimeBuildDirectoriesTests: XCTestCase {
  func testStagingDataLivesUnderUserDataAndNeverInsideSignedSharedSupport() {
    let directories = VoiceFlypyRimeBuildDirectories(
      userData: URL(fileURLWithPath: "/Users/example/Library/Rime-VoiceFlypy", isDirectory: true),
      sharedData: URL(fileURLWithPath: "/Library/Input Methods/VoiceFlypy.app/Contents/SharedSupport", isDirectory: true)
    )

    XCTAssertEqual(
      directories.staging.path,
      "/Users/example/Library/Rime-VoiceFlypy/build"
    )
    XCTAssertTrue(directories.staging.path.hasPrefix(directories.userData.path + "/"))
    XCTAssertFalse(directories.staging.path.hasPrefix(directories.sharedData.path + "/"))
  }
}

final class FlypyCodeResolverTests: XCTestCase {
  func testCanonicalCodesFollowTheActiveRimeFlypyAlgebra() {
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "wen4"), "wf")
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "ti2"), "ti")
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "zui4"), "zv")
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "xin1"), "xb")
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "shi4"), "ui")
    XCTAssertEqual(VoiceFlypyFlypyCodeResolver.canonicalCode(forPinyin: "ju4"), "jv")
  }

  func testAbbreviatedInputHintStillResolvesFullPerGlyphCodes() {
    let readings: [Character: [String]] = [
      "问": ["wen4"], "题": ["ti2"],
      "最": ["zui4"], "新": ["xin1"],
    ]
    XCTAssertEqual(
      VoiceFlypyFlypyCodeResolver.resolve(text: "问题", inputHint: "wft", readings: readings),
      ["wf", "ti"]
    )
    XCTAssertEqual(
      VoiceFlypyFlypyCodeResolver.resolve(text: "最新", inputHint: "zvx", readings: readings),
      ["zv", "xb"]
    )
  }

  func testInputHintDisambiguatesPolyphonicCharacter() {
    let readings: [Character: [String]] = [
      "行": ["xing2", "hang2"], "业": ["ye4"],
    ]
    XCTAssertEqual(
      VoiceFlypyFlypyCodeResolver.resolve(text: "行业", inputHint: "hhy", readings: readings),
      ["hh", "ye"]
    )
  }
}

final class PhraseLearningJournalTests: XCTestCase {
  private let readings: [Character: [String]] = [
    "甲": ["jia3"], "乙": ["yi3"], "丙": ["bing3"], "丁": ["ding1"],
    "戊": ["wu4"], "问": ["wen4"], "题": ["ti2"], "最": ["zui4"], "新": ["xin1"],
  ]

  func testLegacyCommitJournalTracksDeleteInterleavingForUsageTelemetry() {
    var journal = VoiceFlypyPhraseLearningJournal()
    XCTAssertEqual(
      journal.recordCommitted(text: "甲乙", inputHint: "jxy", readings: readings),
      "jxyi"
    )
    XCTAssertTrue(journal.deleteBackward())
    XCTAssertEqual(
      journal.recordCommitted(text: "丙丁", inputHint: "bkd", readings: readings),
      "bkdk"
    )
    XCTAssertTrue(journal.deleteBackward())
    XCTAssertEqual(journal.draft, VoiceFlypyPhraseDraft(text: "甲丙", code: "jxbk"))
  }

  func testCompositionBackspaceCanLeaveCommittedJournalUntouched() {
    var journal = VoiceFlypyPhraseLearningJournal()
    _ = journal.recordCommitted(text: "问题", inputHint: "wft", readings: readings)
    // The controller intentionally does not send composition-internal deletion
    // to the committed-text journal.
    XCTAssertEqual(journal.draft, VoiceFlypyPhraseDraft(text: "问题", code: "wfti"))
  }

  func testUnresolvableCommitFailsClosedAndClearsOldDraft() {
    var journal = VoiceFlypyPhraseLearningJournal()
    _ = journal.recordCommitted(text: "问题", inputHint: "wft", readings: readings)
    XCTAssertNil(journal.recordCommitted(text: "!", inputHint: nil, readings: readings))
    XCTAssertNil(journal.draft)
  }

  func testJournalCanSupplyEachExplicitlySelectedSuffixLength() {
    var journal = VoiceFlypyPhraseLearningJournal()
    _ = journal.recordCommitted(text: "甲乙丙丁戊", inputHint: nil, readings: readings)
    XCTAssertEqual(journal.draft(length: 2)?.text, "丁戊")
    XCTAssertEqual(journal.draft(length: 3)?.text, "丙丁戊")
    XCTAssertEqual(journal.draft(length: 4)?.text, "乙丙丁戊")
    XCTAssertEqual(journal.draft(length: 5)?.text, "甲乙丙丁戊")
  }
}

final class VisiblePhraseExtractorTests: XCTestCase {
  private var basicLexicon: VoiceFlypyPinyinLexicon {
    VoiceFlypyPinyinLexicon(entries: [
      ("甲", "jia3"), ("乙", "yi3"), ("丙", "bing3"),
      ("丁", "ding1"), ("戊", "wu4"), ("己", "ji3"),
    ])
  }

  func testDefaultFallbackExtractsAtMostFiveChineseGlyphs() {
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙"), "甲乙")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙丙"), "甲乙丙")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙丙丁"), "甲乙丙丁")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙丙丁戊"), "甲乙丙丁戊")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙丙丁戊己"), "乙丙丁戊己")
  }

  func testExactLengthSelectionDoesNotGuessTheLongestSuffix() {
    XCTAssertEqual(
      VoiceFlypyVisiblePhraseExtractor.suffix(from: "围偏紧凶", exactLength: 2),
      "紧凶"
    )
    XCTAssertEqual(
      VoiceFlypyVisiblePhraseExtractor.suffix(from: "围偏紧凶", exactLength: 3),
      "偏紧凶"
    )
    XCTAssertEqual(
      VoiceFlypyVisiblePhraseExtractor.suffix(from: "前围偏紧凶", exactLength: 5),
      "前围偏紧凶"
    )
    XCTAssertNil(
      VoiceFlypyVisiblePhraseExtractor.suffix(from: "紧凶", exactLength: 3)
    )
  }

  func testPunctuationAndWhitespaceBoundTheCaretAdjacentRun() {
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "历史，甲乙"), "甲乙")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "历史 甲乙丙"), "甲乙丙")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "历史\n甲乙丙丁"), "甲乙丙丁")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "历史，甲乙。"), "甲乙")
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "历史，甲乙！？  "), "甲乙")
  }

  func testExtractionDependsOnlyOnFinalVisibleTextNotEditHistory() {
    let afterRepeatedDeletes = "曾经输入整词又逐字删除；甲丙"
    let afterPasteAndUndo = "paste/undo:甲丙"
    XCTAssertEqual(
      VoiceFlypyVisiblePhraseExtractor.suffix(from: afterRepeatedDeletes),
      VoiceFlypyVisiblePhraseExtractor.suffix(from: afterPasteAndUndo)
    )
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: afterRepeatedDeletes), "甲丙")
  }

  func testNilShortAndNonCJKCaretSuffixesFailClosed() {
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: nil))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: ""))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲"))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙abc"))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙🙂"))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: "甲乙%"))
    XCTAssertNil(VoiceFlypyVisiblePhraseExtractor.suffix(from: "123"))
  }

  func testSupplementaryPlaneHanIsRecognizedAsOneCharacter() {
    XCTAssertTrue(VoiceFlypyVisiblePhraseExtractor.isCJK("𠀀"))
    XCTAssertEqual(VoiceFlypyVisiblePhraseExtractor.suffix(from: "x𠀀甲"), "𠀀甲")
  }

  func testDraftResolvesFreshFlypyCodesFromFinalVisibleSuffix() {
    XCTAssertEqual(
      basicLexicon.draft(fromVisiblePrefix: "旧文，甲乙丙丁戊"),
      VoiceFlypyPhraseDraft(text: "甲乙丙丁戊", code: "jxyibkdkwu")
    )
    XCTAssertEqual(
      basicLexicon.draft(fromVisiblePrefix: "旧文，甲乙丙丁戊", exactLength: 2),
      VoiceFlypyPhraseDraft(text: "丁戊", code: "dkwu")
    )
    let incomplete = VoiceFlypyPinyinLexicon(entries: [("甲", "jia3")])
    XCTAssertNil(
      incomplete.draft(fromVisiblePrefix: "旧文，甲乙")
    )
  }

  func testPhraseEntriesOverrideWrongFirstReadingsForPolyphonicWords() {
    let lexicon = VoiceFlypyPinyinLexicon(entries: [
      // Put the contextually wrong single-character readings first. Exact
      // phrase entries must win even when there was no raw-input hint.
      ("重", "zhong4"), ("重", "chong2"), ("新", "xin1"),
      ("行", "xing2"), ("行", "hang2"), ("业", "ye4"),
      ("银", "yin2"), ("甲", "jia3"),
      ("重新", "chong2 xin1"),
      ("行业", "hang2 ye4"),
      ("银行", "yin2 hang2"),
    ])

    XCTAssertEqual(lexicon.codes(for: "重新"), ["is", "xb"])
    XCTAssertEqual(lexicon.codes(for: "行业"), ["hh", "ye"])
    XCTAssertEqual(lexicon.codes(for: "银行"), ["yb", "hh"])
    // No phrase row: deterministic fallback is each character's first entry.
    XCTAssertEqual(lexicon.codes(for: "行甲"), ["xk", "jx"])
  }

  func testJournalOnlySuppliesCodeWhenItsTextExactlyMatchesVisibleSuffix() {
    let lexicon = VoiceFlypyPinyinLexicon(entries: [
      ("行", "xing2"), ("行", "hang2"), ("业", "ye4"),
      ("银", "yin2"),
    ])
    let inputTimePronunciation = VoiceFlypyPhraseDraft(text: "行业", code: "hhye")

    XCTAssertEqual(
      lexicon.draft(
        fromVisiblePrefix: "任意历史；行业",
        journalDraft: inputTimePronunciation
      ),
      inputTimePronunciation
    )
    XCTAssertEqual(
      lexicon.draft(
        fromVisiblePrefix: "任意历史；银行",
        journalDraft: inputTimePronunciation
      ),
      VoiceFlypyPhraseDraft(text: "银行", code: "ybxk")
    )
    XCTAssertNil(lexicon.draft(fromVisiblePrefix: nil, journalDraft: inputTimePronunciation))
  }

  func testSaveRevalidationChecksVisibleTextNotHowItsCodeWasResolved() {
    XCTAssertTrue(
      VoiceFlypyVisiblePhraseExtractor.matchesVisibleSuffix(
        text: "行业",
        visiblePrefix: "更长前文行业"
      )
    )
    XCTAssertFalse(
      VoiceFlypyVisiblePhraseExtractor.matchesVisibleSuffix(
        text: "行业",
        visiblePrefix: "前文，银行"
      )
    )
    XCTAssertFalse(
      VoiceFlypyVisiblePhraseExtractor.matchesVisibleSuffix(text: "行业", visiblePrefix: nil)
    )
  }
}

final class SpaceGestureTests: XCTestCase {
  func testDoubleSpaceRequestsAnExplicitLengthOnlyAfterTripleSpaceWindowCloses() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    XCTAssertEqual(gesture.tap(at: 1.0), [])
    XCTAssertEqual(gesture.tap(at: 1.1), [])
    XCTAssertEqual(gesture.settle(), [.requestPhraseLength(fallbackSpaces: 2)])
  }

  func testGestureNeverGuessesAPhraseBoundary() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    XCTAssertEqual(gesture.tap(at: 1.0), [])
    XCTAssertEqual(gesture.tap(at: 1.1), [])
    XCTAssertEqual(gesture.settle(), [.requestPhraseLength(fallbackSpaces: 2)])
  }

  func testCompositionCommitSpaceCountsAsFirstTapWithoutInsertingAnotherSpace() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    XCTAssertEqual(gesture.tap(at: 1.0, fallbackSpaces: 0), [])
    XCTAssertEqual(gesture.settle(), [])

    XCTAssertEqual(gesture.tap(at: 2.0, fallbackSpaces: 0), [])
    XCTAssertEqual(gesture.tap(at: 2.1), [])
    XCTAssertEqual(gesture.settle(), [.requestPhraseLength(fallbackSpaces: 1)])
  }

  func testControllerReceivesTheCorrectFallbackAfterCompositionCommit() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    _ = gesture.tap(at: 1.0, fallbackSpaces: 0)
    _ = gesture.tap(at: 1.1)
    XCTAssertEqual(gesture.settle(), [.requestPhraseLength(fallbackSpaces: 1)])
  }

  func testTripleSpaceEntersChaiziAndNeverLearns() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    _ = gesture.tap(at: 1.0)
    _ = gesture.tap(at: 1.1)
    XCTAssertEqual(gesture.tap(at: 1.2), [.enterChaizi])
    XCTAssertEqual(gesture.settle(), [])
  }

  func testNonSpaceSettlementPreservesOrdering() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    _ = gesture.tap(at: 1.0)
    XCTAssertEqual(gesture.settle(), [.insertSpaces(1)])
  }

  func testSeparatedSpacesAreNotMisclassifiedAsDoubleSpace() {
    var gesture = VoiceFlypySpaceGesture(timeout: 0.5)
    _ = gesture.tap(at: 1.0)
    XCTAssertEqual(gesture.tap(at: 1.7), [.insertSpaces(1)])
    XCTAssertEqual(gesture.settle(), [.insertSpaces(1)])
  }
}

final class RecentCommitTests: XCTestCase {
  private let draft = VoiceFlypyPhraseDraft(text: "甲丙", code: "jxbk")

  func testRecentCommitIsBoundedAndExplicitlyInvalidated() {
    var recent = VoiceFlypyRecentCommit(lifetime: 1.0)
    recent.record(draft, at: 10.0)
    XCTAssertEqual(recent.validDraft(at: 10.9), draft)
    XCTAssertNil(recent.validDraft(at: 11.01))

    recent.record(draft, at: 20.0)
    recent.invalidate()
    XCTAssertNil(recent.validDraft(at: 20.1))
  }
}

final class ChaiziInteractionTests: XCTestCase {
  func testPhysicalBackspaceDetectionDoesNotDependOnModifiers() {
    XCTAssertTrue(
      VoiceFlypyChaiziInteractionRules.isPhysicalBackspace(keyCode: 51, character: nil)
    )
    XCTAssertTrue(
      VoiceFlypyChaiziInteractionRules.isPhysicalBackspace(keyCode: 117, character: "\u{7f}")
    )
    XCTAssertFalse(
      VoiceFlypyChaiziInteractionRules.isPhysicalBackspace(keyCode: 50, character: "`")
    )
  }

  func testBackspaceStateTransitionsAreHostSafe() {
    XCTAssertEqual(
      VoiceFlypyChaiziInteractionRules.backspaceAction(
        hasRawInput: true, componentCount: 2, hasResults: true
      ),
      .deleteRawInput
    )
    XCTAssertEqual(
      VoiceFlypyChaiziInteractionRules.backspaceAction(
        hasRawInput: false, componentCount: 2, hasResults: true
      ),
      .returnToComponents
    )
    XCTAssertEqual(
      VoiceFlypyChaiziInteractionRules.backspaceAction(
        hasRawInput: false, componentCount: 2, hasResults: false
      ),
      .removeLastComponent
    )
    XCTAssertEqual(
      VoiceFlypyChaiziInteractionRules.backspaceAction(
        hasRawInput: false, componentCount: 0, hasResults: false
      ),
      .exit
    )
  }

  func testMarkerRestoresOnlyForActiveModeWithoutRawInput() {
    XCTAssertTrue(
      VoiceFlypyChaiziInteractionRules.shouldRestoreMarker(isActive: true, hasRawInput: false)
    )
    XCTAssertFalse(
      VoiceFlypyChaiziInteractionRules.shouldRestoreMarker(isActive: true, hasRawInput: true)
    )
    XCTAssertFalse(
      VoiceFlypyChaiziInteractionRules.shouldRestoreMarker(isActive: false, hasRawInput: false)
    )
  }

  func testLiteralTildeIsAnExplicitExit() {
    XCTAssertTrue(VoiceFlypyChaiziInteractionRules.isTildeExit("~"))
    XCTAssertFalse(VoiceFlypyChaiziInteractionRules.isTildeExit("`"))
    XCTAssertFalse(VoiceFlypyChaiziInteractionRules.isTildeExit(nil))
  }

  func testLearnedPhraseDeploymentRequiresACompletelyIdleActiveController() {
    let safe = VoiceFlypyLearnedPhraseDeploymentRules.isSafe(
      hasValidSession: true,
      voiceArmed: false,
      voiceActive: false,
      chaiziMode: false,
      pendingSpaceTapCount: 0,
      hasRawInput: false
    )
    XCTAssertTrue(safe)

    let blockers: [(Bool, Bool, Bool, Bool, Int, Bool)] = [
      (false, false, false, false, 0, false),
      (true, true, false, false, 0, false),
      (true, false, true, false, 0, false),
      (true, false, false, true, 0, false),
      (true, false, false, false, 1, false),
      (true, false, false, false, 0, true),
    ]
    for blocker in blockers {
      XCTAssertFalse(
        VoiceFlypyLearnedPhraseDeploymentRules.isSafe(
          hasValidSession: blocker.0,
          voiceArmed: blocker.1,
          voiceActive: blocker.2,
          chaiziMode: blocker.3,
          pendingSpaceTapCount: blocker.4,
          hasRawInput: blocker.5
        )
      )
    }
  }

  func testPrebuiltIndexPreservesExactCanonicalAndSubsetRanking() {
    let index = VoiceFlypyChaiziIndex(definitions: [
      "林": ["木", "木"],
      "休": ["亻", "木"],
      "本": ["木", "一"],
      "体": ["亻", "本"],
      "品": ["口", "口", "口"],
    ])

    XCTAssertEqual(index.lookup(components: ["木", "木"]).first, "林")
    XCTAssertEqual(index.lookup(components: ["人", "木"]).first, "休")
    let woodAndOne = index.lookup(components: ["木", "一"])
    XCTAssertEqual(woodAndOne.first, "本")
    XCTAssertTrue(woodAndOne.contains("体"))
    XCTAssertEqual(index.lookup(components: ["口", "口", "口"]).first, "品")
  }
}

final class LearningStoreTests: XCTestCase {
  func testLearnedPhraseStoreCreatesAndDeduplicatesDynamicFile() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("voiceflypy_learned_phrases.txt")
      var notifications = 0
      let store = VoiceFlypyLearnedPhraseStore(url: path) { _, _ in notifications += 1 }
      XCTAssertTrue(try store.save(phrase: "甲丙", code: "jxbk"))
      XCTAssertFalse(try store.save(phrase: "甲丙", code: "jxbk"))
      XCTAssertEqual(notifications, 1)
      XCTAssertTrue(try String(contentsOf: path).contains("甲丙\tjxbk\t200"))
    }
  }

  func testLearnedPhraseStoreAcceptsAnExplicitFiveCharacterChoice() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("voiceflypy_learned_phrases.txt")
      let store = VoiceFlypyLearnedPhraseStore(url: path, notifier: { _, _ in })
      XCTAssertTrue(try store.save(phrase: "甲乙丙丁戊", code: "jxyibkdkwu"))
      XCTAssertTrue(try String(contentsOf: path).contains("甲乙丙丁戊\tjxyibkdkwu\t200"))
    }
  }

  func testMalformedLearnedFileIsPreserved() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("voiceflypy_learned_phrases.txt")
      let original = Data("bad row\n".utf8)
      try original.write(to: path)
      let store = VoiceFlypyLearnedPhraseStore(url: path, notifier: { _, _ in })
      XCTAssertThrowsError(try store.save(phrase: "甲丙", code: "jxbk"))
      XCTAssertEqual(try Data(contentsOf: path), original)
    }
  }

  func testLegacySingleCharacterRowDoesNotBlockNewPhraseLearning() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("voiceflypy_learned_phrases.txt")
      try Data("单\tuj\t500\n".utf8).write(to: path)
      let store = VoiceFlypyLearnedPhraseStore(url: path, notifier: { _, _ in })
      XCTAssertTrue(try store.save(phrase: "甲丙", code: "jxbk"))
      let content = try String(contentsOf: path)
      XCTAssertTrue(content.contains("单\tuj\t500"))
      XCTAssertTrue(content.contains("甲丙\tjxbk\t200"))
    }
  }

  func testPersonalUsageFlushMergesFreshExternalState() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("personal_usage.json")
      let first = VoiceFlypyPersonalUsageStore(url: path, notifier: {})
      let second = VoiceFlypyPersonalUsageStore(url: path, notifier: {})
      first.record(text: "问题", code: "wfti", explicit: false)
      XCTAssertTrue(first.flushSynchronously())
      second.record(text: "最新", code: "zvxb", explicit: true)
      XCTAssertTrue(second.flushSynchronously())
      let document = try JSONDecoder().decode(
        VoiceFlypyPersonalUsageStore.Document.self,
        from: Data(contentsOf: path)
      )
      XCTAssertEqual(document.terms["问题"]?.selection_count, 1)
      XCTAssertEqual(document.terms["最新"]?.explicit_count, 1)
    }
  }

  func testPersonalUsagePublishesAnAtomicLocalRuntimeMirror() throws {
    try withTemporaryDirectory { directory in
      let primary = directory.appendingPathComponent("sync/personal_usage.json")
      let mirror = directory.appendingPathComponent("runtime/personal_usage.json")
      let store = VoiceFlypyPersonalUsageStore(
        url: primary,
        mirrorURL: mirror,
        notifier: {}
      )
      store.record(text: "紧凶", code: "jbxs", explicit: true)
      XCTAssertTrue(store.flushSynchronously())
      XCTAssertEqual(try Data(contentsOf: mirror), try Data(contentsOf: primary))
      let document = try JSONDecoder().decode(
        VoiceFlypyPersonalUsageStore.Document.self,
        from: Data(contentsOf: mirror)
      )
      XCTAssertEqual(document.terms["紧凶"]?.explicit_count, 1)
    }
  }

  func testExistingSyncedUsageSeedsMirrorAndNotifiesOnlyWhenChanged() throws {
    try withTemporaryDirectory { directory in
      let primary = directory.appendingPathComponent("personal_usage.json")
      let mirror = directory.appendingPathComponent("runtime/personal_usage.json")
      let document = VoiceFlypyPersonalUsageStore.Document(terms: [
        "紧凶": .init(
          flypy_code: "jbxs",
          selection_count: 2,
          explicit_count: 0,
          updated_at: "2026-09-08T00:00:00Z"
        ),
      ])
      let encoder = JSONEncoder()
      encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
      try encoder.encode(document).write(to: primary)
      var notifications = 0
      let store = VoiceFlypyPersonalUsageStore(
        url: primary,
        mirrorURL: mirror,
        notifier: { notifications += 1 }
      )

      XCTAssertTrue(store.refreshMirrorSynchronously())
      XCTAssertEqual(notifications, 1)
      XCTAssertTrue(store.refreshMirrorSynchronously())
      XCTAssertEqual(notifications, 1)
      XCTAssertEqual(try Data(contentsOf: mirror), try Data(contentsOf: primary))
    }
  }

  func testInvalidSyncedUsageNeverOverwritesLastGoodRuntimeMirror() throws {
    try withTemporaryDirectory { directory in
      let primary = directory.appendingPathComponent("personal_usage.json")
      let mirror = directory.appendingPathComponent("runtime/personal_usage.json")
      try FileManager.default.createDirectory(
        at: mirror.deletingLastPathComponent(),
        withIntermediateDirectories: true
      )
      let lastGood = Data("{\"last_good\":true}".utf8)
      try lastGood.write(to: mirror)
      try Data("{broken".utf8).write(to: primary)
      let store = VoiceFlypyPersonalUsageStore(
        url: primary,
        mirrorURL: mirror,
        notifier: {}
      )

      XCTAssertFalse(store.refreshMirrorSynchronously())
      XCTAssertEqual(try Data(contentsOf: mirror), lastGood)
    }
  }

  func testMissingSyncedUsagePreservesExistingRuntimeMirror() throws {
    try withTemporaryDirectory { directory in
      let primary = directory.appendingPathComponent("missing/personal_usage.json")
      let mirror = directory.appendingPathComponent("runtime/personal_usage.json")
      try FileManager.default.createDirectory(
        at: mirror.deletingLastPathComponent(),
        withIntermediateDirectories: true
      )
      let lastGood = Data("{\"last_good\":true}".utf8)
      try lastGood.write(to: mirror)
      var notifications = 0
      let store = VoiceFlypyPersonalUsageStore(
        url: primary,
        mirrorURL: mirror,
        notifier: { notifications += 1 }
      )

      XCTAssertTrue(store.refreshMirrorSynchronously())
      XCTAssertEqual(try Data(contentsOf: mirror), lastGood)
      XCTAssertEqual(notifications, 0)
    }
  }

  func testCorruptPersonalUsageFileIsNeverOverwritten() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("personal_usage.json")
      let original = Data("{not-json".utf8)
      try original.write(to: path)
      let store = VoiceFlypyPersonalUsageStore(url: path, notifier: {})
      store.record(text: "问题", code: "wfti", explicit: false)
      XCTAssertFalse(store.flushSynchronously())
      XCTAssertEqual(try Data(contentsOf: path), original)
    }
  }

  func testPersonalUsageCounterOverflowPreservesOriginalInsteadOfTrapping() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("personal_usage.json")
      let document = VoiceFlypyPersonalUsageStore.Document(terms: [
        "问题": .init(
          flypy_code: "wfti",
          selection_count: Int.max,
          explicit_count: 0,
          updated_at: "2026-09-03T00:00:00Z"
        ),
      ])
      let original = try JSONEncoder().encode(document)
      try original.write(to: path)
      let store = VoiceFlypyPersonalUsageStore(url: path, notifier: {})
      store.record(text: "问题", code: "wfti", explicit: false)
      XCTAssertFalse(store.flushSynchronously())
      XCTAssertEqual(try Data(contentsOf: path), original)
    }
  }

  func testLearnedPhraseStoreLockContentionHasABoundedFailure() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("voiceflypy_learned_phrases.txt")
      let descriptor = try holdStoreLock(for: path)
      defer { releaseStoreLock(descriptor) }
      let store = VoiceFlypyLearnedPhraseStore(
        url: path,
        lockTimeout: 0.03,
        notifier: { _, _ in }
      )
      let startedAt = ProcessInfo.processInfo.systemUptime
      XCTAssertThrowsError(try store.save(phrase: "甲丙", code: "jxbk"))
      XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - startedAt, 0.5)
      XCTAssertFalse(FileManager.default.fileExists(atPath: path.path))
    }
  }

  func testSynchronousPersonalFlushReturnsAtItsOwnDeadline() throws {
    try withTemporaryDirectory { directory in
      let path = directory.appendingPathComponent("personal_usage.json")
      let descriptor = try holdStoreLock(for: path)
      let store = VoiceFlypyPersonalUsageStore(url: path, lockTimeout: 1, notifier: {})
      store.record(text: "问题", code: "wfti", explicit: true)
      let startedAt = ProcessInfo.processInfo.systemUptime
      XCTAssertFalse(store.flushSynchronously(timeout: 0.03))
      XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - startedAt, 0.5)

      releaseStoreLock(descriptor)
      XCTAssertTrue(store.flushSynchronously(timeout: 1.5))
    }
  }

  private func holdStoreLock(for target: URL) throws -> Int32 {
    let lockPath = target.deletingLastPathComponent()
      .appendingPathComponent(".\(target.lastPathComponent).lock")
    let descriptor = lockPath.path.withCString {
      Darwin.open($0, O_CREAT | O_RDWR | O_CLOEXEC, S_IRUSR | S_IWUSR)
    }
    guard descriptor >= 0 else {
      throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
    guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
      Darwin.close(descriptor)
      throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }
    return descriptor
  }

  private func releaseStoreLock(_ descriptor: Int32) {
    _ = flock(descriptor, LOCK_UN)
    Darwin.close(descriptor)
  }

  private func withTemporaryDirectory(_ body: (URL) throws -> Void) throws {
    let directory = FileManager.default.temporaryDirectory
      .appendingPathComponent("VoiceFlypyLearningTests-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    try body(directory)
  }
}
