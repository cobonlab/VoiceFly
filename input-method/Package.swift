// swift-tools-version: 5.10

import PackageDescription

let package = Package(
  name: "VoiceFlyCore",
  platforms: [.macOS(.v13)],
  products: [
    .library(name: "VoiceFlyCore", targets: ["VoiceFlyCore"]),
    .executable(name: "VoiceFlySettings", targets: ["VoiceFlySettings"]),
  ],
  targets: [
    .target(name: "VoiceFlyCore", path: "VoiceFlyCore"),
    .executableTarget(name: "VoiceFlySettings", dependencies: ["VoiceFlyCore"], path: "settings"),
    .testTarget(
      name: "VoiceFlyCoreTests",
      dependencies: ["VoiceFlyCore"],
      path: "tests/VoiceFlyCoreTests"
    ),
  ]
)
