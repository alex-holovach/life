// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Life",
    platforms: [.macOS(.v13), .iOS(.v17)],
    products: [.library(name: "WhoopCore", targets: ["WhoopCore"])],
    targets: [
        .target(name: "WhoopCore", linkerSettings: [.linkedLibrary("sqlite3")]),
        .executableTarget(name: "WhoopCollector", dependencies: ["WhoopCore"], resources: [.process("Resources")]),
        .executableTarget(name: "CoreChecks", dependencies: ["WhoopCore"], path: "Checks"),
        .testTarget(name: "WhoopCoreTests", dependencies: ["WhoopCore"]),
        .testTarget(name: "WhoopCollectorTests", dependencies: ["WhoopCollector"])
    ]
)
