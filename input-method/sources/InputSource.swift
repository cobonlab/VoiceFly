//
//  InputSource.swift
//  Squirrel
//
//  Created by Leo Liu on 5/10/24.
//

import Foundation
import InputMethodKit

final class SquirrelInstaller {
  private static let rootInputSourceID = "com.simonli.inputmethod.VoiceFly"
  enum InputMode: String, CaseIterable {
    static let primary = Self.hans
    case hans = "com.simonli.inputmethod.VoiceFly.Hans"
  }
  private lazy var inputSources: [String: TISInputSource] = {
    var inputSources = [String: TISInputSource]()
    var matchingSources = [InputMode: TISInputSource]()
    let sourceList = TISCreateInputSourceList(nil, true).takeRetainedValue() as! [TISInputSource]
    for inputSource in sourceList {
      let sourceIDRef = TISGetInputSourceProperty(inputSource, kTISPropertyInputSourceID)
      guard let sourceID = unsafeBitCast(sourceIDRef, to: CFString?.self) as String? else { continue }
      inputSources[sourceID] = inputSource
    }
    return inputSources
  }()

  func enabledModes() -> [InputMode] {
    var enabledModes = Set<InputMode>()
    for (mode, inputSource) in getInputSource(modes: InputMode.allCases) {
      if let enabled = getBool(for: inputSource, key: kTISPropertyInputSourceIsEnabled), enabled {
        enabledModes.insert(mode)
      }
      if enabledModes.count == InputMode.allCases.count {
        break
      }
    }
    return Array(enabledModes)
  }

  func register() {
    let error = TISRegisterInputSource(SquirrelApp.appDir as CFURL)
    print("Registration \(error == noErr ? "succeeds" : "fails") from \(SquirrelApp.appDir)")
  }

  func enable(modes: [InputMode] = []) {
    // On macOS 26 a mode may appear enabled while its parent input method is
    // disabled.  Enabling the parent first is required for System Settings to
    // expose the input method and for the mode to be reliably selectable.
    if let rootInputSource = inputSources[Self.rootInputSourceID] {
      let error = TISEnableInputSource(rootInputSource)
      print("Enable \(error == noErr ? "succeeds" : "fails") for input source: \(Self.rootInputSourceID)")
    }
    let modesToEnable = modes.isEmpty ? [.primary] : modes
    for (mode, inputSource) in getInputSource(modes: modesToEnable) {
      let error = TISEnableInputSource(inputSource)
      print("Enable \(error == noErr ? "succeeds" : "fails") for input source: \(mode.rawValue)")
    }
  }

  func select(mode: InputMode? = nil) {
    let enabledInputModes = enabledModes()
    let modeToSelect = mode ?? .primary
    if !enabledInputModes.contains(modeToSelect) {
      if mode != nil {
        enable(modes: [modeToSelect])
      } else {
        print("Default method not enabled yet: \(modeToSelect.rawValue)")
        return
      }
    }
    for (mode, inputSource) in getInputSource(modes: [modeToSelect]) {
      if let enabled = getBool(for: inputSource, key: kTISPropertyInputSourceIsEnabled),
         let selectable = getBool(for: inputSource, key: kTISPropertyInputSourceIsSelectCapable),
         let selected = getBool(for: inputSource, key: kTISPropertyInputSourceIsSelected),
         enabled && selectable && !selected {
        let error = TISSelectInputSource(inputSource)
        print("Selection \(error == noErr ? "succeeds" : "fails") for input source: \(mode.rawValue)")
      } else {
        print("Failed to select \(mode.rawValue)")
      }
    }
  }

  static func currentInputSourceID() -> String? {
    let source = TISCopyCurrentKeyboardInputSource().takeRetainedValue()
    let idRef = TISGetInputSourceProperty(source, kTISPropertyInputSourceID)
    return unsafeBitCast(idRef, to: CFString?.self) as String?
  }

  func disable(modes: [InputMode] = []) {
    let modesToDisable = modes.isEmpty ? InputMode.allCases : modes
    for (mode, inputSource) in getInputSource(modes: modesToDisable) {
      if let enabled = getBool(for: inputSource, key: kTISPropertyInputSourceIsEnabled), enabled {
        let error = TISDisableInputSource(inputSource)
        print("Disable \(error == noErr ? "succeeds" : "fails") for input source: \(mode.rawValue)")
      }
    }
  }

  private func getInputSource(modes: [InputMode]) -> [InputMode: TISInputSource] {
    var matchingSources = [InputMode: TISInputSource]()
    for mode in modes {
      if let inputSource = inputSources[mode.rawValue] {
        matchingSources[mode] = inputSource
      }
    }
    return matchingSources
  }

  private func getBool(for inputSource: TISInputSource, key: CFString!) -> Bool? {
    let enabledRef = TISGetInputSourceProperty(inputSource, key)
    guard let enabled = unsafeBitCast(enabledRef, to: CFBoolean?.self) else { return nil }
    return CFBooleanGetValue(enabled)
  }
}
