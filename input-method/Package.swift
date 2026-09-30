// swift-tools-version: 5.10

import PackageDescription

let package = Package(
  name: "VoiceFlypyCore",
  platforms: [.macOS(.v13)],
  products: [
    .library(name: "VoiceFlypyCore", targets: ["VoiceFlypyCore"]),
  ],
  targets: [
    .target(name: "VoiceFlypyCore", path: "VoiceFlypyCore"),
    .testTarget(
      name: "VoiceFlypyCoreTests",
      dependencies: ["VoiceFlypyCore"],
      path: "tests/VoiceFlypyCoreTests"
    ),
  ]
)
