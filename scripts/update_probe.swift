// Read-only guard for an existing-install update. Never selects an input source.
import Foundation
import Carbon

let source = TISCopyCurrentKeyboardInputSource().takeRetainedValue()
let pointer = TISGetInputSourceProperty(source, kTISPropertyInputSourceID)
let current = unsafeBitCast(pointer, to: CFString?.self) as String? ?? ""
let statusURL = FileManager.default.homeDirectoryForCurrentUser
  .appendingPathComponent("Library/Application Support/VoiceFly/service-status.json")
var phase = "unknown"
var fresh = false
if let data = try? Data(contentsOf: statusURL),
   let status = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
  phase = status["phase"] as? String ?? "unknown"
  let attributes = try? FileManager.default.attributesOfItem(atPath: statusURL.path)
  if let modified = attributes?[.modificationDate] as? Date {
    fresh = abs(modified.timeIntervalSinceNow) < 15
  }
}
let ready = current == "com.apple.keylayout.ABC" && phase == "idle" && fresh
let result: [String: Any] = ["currentSource": current, "voicePhase": phase,
                             "voiceStatusFresh": fresh, "ready": ready]
let data = try! JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(decoding: data, as: UTF8.self))
if CommandLine.arguments.contains("--ready") && !ready { exit(1) }
