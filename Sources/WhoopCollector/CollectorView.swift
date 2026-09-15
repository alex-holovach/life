import SwiftUI
import Charts
import WhoopCore

enum LifeTheme {
    static let canvas = Color(red: 0.047, green: 0.078, blue: 0.067)
    static let card = Color(red: 0.094, green: 0.137, blue: 0.118)
    static let mint = Color(red: 0.827, green: 0.961, blue: 0.682)
    static let text = Color(red: 0.937, green: 0.953, blue: 0.906)
    static let muted = Color(red: 0.64, green: 0.71, blue: 0.66)
    static let coral = Color(red: 1, green: 0.56, blue: 0.47)
    static var assets: Bundle {
        #if SWIFT_PACKAGE
        return .module
        #else
        return .main
        #endif
    }
}

private struct LifeCard: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(22).background(LifeTheme.card, in: RoundedRectangle(cornerRadius: 28))
            .overlay(RoundedRectangle(cornerRadius: 28).stroke(.white.opacity(0.055), lineWidth: 1))
    }
}

struct CollectorView: View {
    @ObservedObject var collector = Services.shared.collector
    @ObservedObject var uploader = Services.shared.uploader
    @Environment(\.scenePhase) private var phase
    @State private var settingsPresented = false
    @StateObject private var temperature = TemperatureClient()
    @StateObject private var hrv = HRVClient()
    @StateObject private var vitals = VitalsClient()
    @StateObject private var wellness = WellnessClient()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                scoreCards
                TimelineView(.periodic(from: .now, by: 5)) { context in
                    VStack(spacing: 18) {
                        HeartRateCard(readings: collector.heartRateReadings,
                                      current: collector.heartRate, stopped: collector.isStopped, now: context.date)
                        WellnessCard(client: wellness, now: context.date)
                        DataQualityCard(report: hrv.snapshot?.quality_report, message: hrv.message, now: context.date)
                        HRVCard(snapshot: hrv.snapshot, message: hrv.message, now: context.date)
                        VitalsCards(snapshot: vitals.snapshot, message: vitals.message, now: context.date)
                        deviceCards(now: context.date)
                        TemperatureCard(snapshot: temperature.snapshot, message: temperature.message, now: context.date)
                    }
                }
                exportCard
                Label(collector.historyStatus, systemImage: "clock.arrow.circlepath")
                    .font(.caption).foregroundStyle(LifeTheme.muted).padding(.horizontal, 8)
                dashboardLink
                if let error = collector.error {
                    Label(error, systemImage: "exclamationmark.circle")
                        .font(.footnote).foregroundStyle(LifeTheme.coral)
                        .padding(.horizontal, 8).accessibilityLabel("Attention: \(error)")
                }
                Text("YOUR DATA. YOUR LIFE.")
                    .font(.system(size: 10, weight: .medium)).tracking(3)
                    .foregroundStyle(LifeTheme.muted.opacity(0.7))
                    .frame(maxWidth: .infinity).padding(.top, 12).padding(.bottom, 20)
            }
            .padding(.horizontal, 22).padding(.top, 12)
            .frame(maxWidth: 620)
            .frame(maxWidth: .infinity)
        }
        .background(LifeTheme.canvas)
        .foregroundStyle(LifeTheme.text)
        .tint(LifeTheme.mint)
        .preferredColorScheme(.dark)
        .sheet(isPresented: $settingsPresented) {
            NavigationStack {
                CollectorSettingsView()
                    .toolbar { ToolbarItem(placement: .confirmationAction) {
                        Button("Done") { settingsPresented = false }.tint(LifeTheme.mint)
                    } }
            }
        }
        .onChange(of: phase) { value in if value == .active { uploader.sync(force: true) } }
        .onAppear { uploader.sync() }
        .task(id: phase) {
            guard phase == .active else { return }
            while !Task.isCancelled {
                async let temperatureRefresh: () = temperature.refresh()
                async let hrvRefresh: () = hrv.refresh()
                async let vitalsRefresh: () = vitals.refresh()
                async let wellnessRefresh: () = wellness.refresh()
                _ = await (temperatureRefresh, hrvRefresh, vitalsRefresh, wellnessRefresh)
                do { try await Task.sleep(nanoseconds: 60_000_000_000) } catch { return }
            }
        }
    }

    private var scoreCards: some View {
        HStack(spacing: 10) {
            TimelineView(.periodic(from: .now, by: 5)) { context in
                let available = hrv.message == nil && hrv.snapshot?.isAvailable(at: context.date) == true
                scoreCard("HRV · last", value: available ? String(format: "%.0f", hrv.snapshot!.rmssd_ms!) : "—", unit: "ms", symbol: "waveform.path", color: LifeTheme.mint,
                          detail: available ? Date(timeIntervalSince1970: hrv.snapshot!.end!).formatted(date: .omitted, time: .shortened) : "No reading")
            }
            TimelineView(.periodic(from: .now, by: 5)) { context in
                scoreCard("Strain", value: wellness.load(at: context.date)?.score.map { String(format: "%.1f", $0) } ?? "—", unit: "/ 21", symbol: "flame", color: LifeTheme.coral, detail: "Today")
            }
            TimelineView(.periodic(from: .now, by: 5)) { context in
                scoreCard("Sleep", value: wellness.sleep(at: context.date)?.score.map { String(format: "%.0f", $0) } ?? "—", unit: "%", symbol: "moon.stars", color: Color(red: 0.75, green: 0.72, blue: 0.96), detail: "Last sleep")
            }
        }
    }

    private func scoreCard(_ title: String, value: String, unit: String, symbol: String, color: Color, detail: String? = nil) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 5) {
                Image(systemName: symbol).font(.system(size: 11, weight: .medium))
                Text(title).font(.system(size: 11, weight: .medium))
            }.foregroundStyle(color)
            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(value).font(.system(size: 27, weight: .regular, design: .rounded)).monospacedDigit()
                Text(unit).font(.system(size: 9)).foregroundStyle(LifeTheme.muted)
            }.lineLimit(1).minimumScaleFactor(0.7)
            if let detail { Text(detail).font(.system(size: 9)).foregroundStyle(LifeTheme.muted) }
        }.frame(maxWidth: .infinity, alignment: .leading).padding(15)
            .background(color.opacity(0.065), in: RoundedRectangle(cornerRadius: 22))
            .overlay(RoundedRectangle(cornerRadius: 22).stroke(color.opacity(0.1), lineWidth: 1))
            .accessibilityElement(children: .combine)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image("LifeMark", bundle: LifeTheme.assets).resizable().scaledToFit()
                .frame(width: 46, height: 46).clipShape(RoundedRectangle(cornerRadius: 14))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Life").font(.system(size: 32, weight: .medium, design: .rounded)).tracking(-1)
                Text(Date.now, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                    .font(.caption).foregroundStyle(LifeTheme.muted)
            }
            Spacer()
            Button { settingsPresented = true } label: {
                Image(systemName: "slider.horizontal.3").font(.system(size: 19, weight: .medium))
                    .frame(width: 46, height: 46)
                    .background(.white.opacity(0.055), in: Circle())
            }
            .foregroundStyle(LifeTheme.text).accessibilityLabel("Settings")
        }.padding(.bottom, 8)
    }

    private func deviceCards(now: Date) -> some View {
        let battery = collector.batteryEvidence.verified(at: now)
        let connected = !collector.isStopped && collector.lastSeen.map { (0...10).contains(now.timeIntervalSince($0)) } == true
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .top, spacing: 12) {
                connectionCard(connected: connected)
                batteryCard(battery: battery, now: now)
            }
            VStack(spacing: 12) {
                connectionCard(connected: connected)
                batteryCard(battery: battery, now: now)
            }
        }
    }

    private func connectionCard(connected: Bool) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Image(systemName: "wave.3.right").font(.system(size: 18))
                Spacer()
                Circle().fill(connected ? LifeTheme.mint : LifeTheme.muted).frame(width: 6, height: 6)
            }.foregroundStyle(LifeTheme.mint)
            VStack(alignment: .leading, spacing: 5) {
                Text("WHOOP 4.0").font(.system(size: 16, weight: .semibold))
                Text(connected ? "Connected" : collector.status)
                    .font(.caption).foregroundStyle(LifeTheme.muted).fixedSize(horizontal: false, vertical: true)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).modifier(LifeCard())
            .accessibilityElement(children: .combine)
    }

    private func batteryCard(battery: BatteryEvidence.Reading?, now: Date) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Image(systemName: battery.map { $0.percent <= 20 ? "battery.25percent" : "battery.75percent" } ?? "battery.0percent")
                .font(.system(size: 18)).foregroundStyle(battery.map { $0.percent <= 20 ? LifeTheme.coral : LifeTheme.mint } ?? LifeTheme.muted)
            VStack(alignment: .leading, spacing: 5) {
                Text(battery.map { "\(Int($0.percent.rounded()))%" } ?? "—")
                    .font(.system(size: 16, weight: .semibold)).monospacedDigit()
                Text(battery == nil ? "Battery unverified" : "Battery · verified")
                    .font(.caption).foregroundStyle(LifeTheme.muted).fixedSize(horizontal: false, vertical: true)
            }
        }.frame(maxWidth: .infinity, alignment: .leading).modifier(LifeCard())
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Battery: \(collector.batteryEvidence.status(at: now))")
    }

    private var exportCard: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "arrow.up.right.cloud").font(.system(size: 22))
                    .foregroundStyle(LifeTheme.mint).padding(.top, 2)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Safe with you").font(.system(size: 18, weight: .medium))
                    Text(uploader.status).font(.caption).foregroundStyle(LifeTheme.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("\(uploader.pendingCount) queued").font(.system(size: 14, weight: .semibold)).monospacedDigit()
                    if let date = uploader.lastAcknowledged {
                        Text("Saved to backend \(date.formatted(date: .omitted, time: .shortened))")
                            .font(.caption2).foregroundStyle(LifeTheme.muted)
                    } else {
                        Text("Waiting for first backend upload").font(.caption2).foregroundStyle(LifeTheme.muted)
                    }
                }
                Spacer(minLength: 0)
                Button { uploader.sync(force: true) } label: {
                    Text("Upload now").font(.system(size: 13, weight: .semibold))
                        .padding(.horizontal, 16).frame(minHeight: 44)
                        .foregroundStyle(LifeTheme.canvas).background(LifeTheme.mint, in: Capsule())
                }.buttonStyle(.plain)
            }
            if let retry = uploader.retryAt {
                Text("Next retry eligible \(retry.formatted(date: .omitted, time: .shortened))")
                    .font(.caption2).foregroundStyle(LifeTheme.coral)
            }
        }.modifier(LifeCard())
    }

    @ViewBuilder private var dashboardLink: some View {
        if let url = AppConfiguration.dashboardURL {
        Link(destination: url) {
            HStack(spacing: 14) {
                Image(systemName: "chart.xyaxis.line").font(.system(size: 20)).foregroundStyle(LifeTheme.mint)
                VStack(alignment: .leading, spacing: 5) {
                    Text("The bigger picture").font(.system(size: 17, weight: .medium))
                    Text("Open full dashboard").font(.caption).foregroundStyle(LifeTheme.muted)
                }
                Spacer()
                Image(systemName: "arrow.up.right").font(.system(size: 17)).foregroundStyle(LifeTheme.mint)
            }.padding(.horizontal, 8).padding(.vertical, 16)
        }.foregroundStyle(LifeTheme.text)
            .accessibilityLabel("Open full dashboard in Grafana")
            .accessibilityHint("Requires Tailscale and your Grafana login")
        }
    }
}

private struct HeartRateCard: View {
    let readings: [HeartRateReading]
    let current: Int?
    let stopped: Bool
    let now: Date
    @State private var minutes = 5
    @State private var selectedAt: Date?
    private var start: Date { now.addingTimeInterval(-Double(minutes * 60)) }
    private var visible: [HeartRateReading] { readings.filter { $0.at >= start && $0.at <= now } }
    private var fresh: Bool {
        !stopped && (current ?? 0) > 0 && readings.last.map { (0...10).contains(now.timeIntervalSince($0.at)) } == true
    }
    private var selected: HeartRateReading? {
        guard let selectedAt,
              let nearest = visible.min(by: { abs($0.at.timeIntervalSince(selectedAt)) < abs($1.at.timeIntervalSince(selectedAt)) }),
              abs(nearest.at.timeIntervalSince(selectedAt)) <= 8 else { return nil }
        return nearest
    }
    private var extent: ClosedRange<Double> {
        let values = visible.map(\.bpm)
        let low = Double(values.min() ?? 60), high = Double(values.max() ?? 100)
        return max(0, floor((low - 10) / 10) * 10)...(ceil((high + 10) / 10) * 10)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Label("HEART RATE", systemImage: "heart.fill")
                    .font(.system(size: 10, weight: .semibold)).tracking(1.8)
                    .foregroundStyle(LifeTheme.coral)
                Spacer()
                HStack(spacing: 5) {
                    Circle().fill(fresh ? LifeTheme.mint : LifeTheme.muted).frame(width: 5, height: 5)
                    Text(fresh ? "LIVE" : stopped ? "PAUSED" : "WAITING")
                        .font(.system(size: 9, weight: .semibold)).tracking(1.2)
                }.foregroundStyle(fresh ? LifeTheme.mint : LifeTheme.muted)
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(selected.map { String($0.bpm) } ?? (fresh ? String(current!) : "—"))
                    .font(.system(size: 80, weight: .light, design: .rounded)).tracking(-4).monospacedDigit()
                    .foregroundStyle(LifeTheme.mint).contentTransition(.numericText())
                Text("bpm").font(.system(size: 18)).foregroundStyle(LifeTheme.muted)
                Spacer()
            }.padding(.top, 16)
            Text(selected.map { $0.at.formatted(date: .omitted, time: .standard) } ?? (fresh ? "Your rhythm, right now." : stopped ? "Ready whenever you are." : "Waiting for a fresh reading."))
                .font(.system(size: 13)).foregroundStyle(LifeTheme.muted).padding(.top, 2)
            chart.padding(.top, 26)
            HStack(spacing: 6) {
                ForEach([5, 30, 60], id: \.self) { range in
                    Button { minutes = range; selectedAt = nil } label: {
                        Text(range == 60 ? "1 hour" : "\(range) min")
                            .font(.system(size: 12, weight: .medium))
                            .frame(maxWidth: .infinity).frame(minHeight: 36)
                            .background(minutes == range ? LifeTheme.mint.opacity(0.13) : .clear, in: Capsule())
                            .foregroundStyle(minutes == range ? LifeTheme.mint : LifeTheme.muted)
                    }.buttonStyle(.plain)
                        .accessibilityAddTraits(minutes == range ? [.isSelected] : [])
                }
            }.padding(4).background(LifeTheme.canvas.opacity(0.6), in: Capsule()).padding(.top, 18)
            if !visible.isEmpty {
                HStack {
                    statistic("LOW", value: visible.map(\.bpm).min()!)
                    Spacer()
                    statistic("AVERAGE", value: Int((Double(visible.reduce(0) { $0 + $1.bpm }) / Double(visible.count)).rounded()))
                    Spacer()
                    statistic("HIGH", value: visible.map(\.bpm).max()!)
                }.padding(.top, 22)
            }
        }.modifier(LifeCard())
    }

    @ViewBuilder private var chart: some View {
        if visible.isEmpty {
            VStack(spacing: 9) {
                Image(systemName: "waveform.path").font(.system(size: 30, weight: .ultraLight)).foregroundStyle(LifeTheme.coral.opacity(0.7))
                Text("A little time, a clearer picture.").font(.system(size: 13))
                Text("Your heart rate will appear here as Life collects.")
                    .font(.caption2).foregroundStyle(LifeTheme.muted).multilineTextAlignment(.center)
            }.frame(maxWidth: .infinity).frame(height: 145)
        } else {
            Chart {
                ForEach(HeartRateTimeline.points(visible)) { point in
                    LineMark(x: .value("Time", point.reading.at), y: .value("Heart rate", point.reading.bpm), series: .value("Continuous readings", point.segment))
                        .foregroundStyle(LifeTheme.coral).lineStyle(StrokeStyle(lineWidth: 2.2, lineCap: .round, lineJoin: .round))
                        .interpolationMethod(.linear)
                }
                if let last = visible.last {
                    PointMark(x: .value("Time", last.at), y: .value("Heart rate", last.bpm))
                        .foregroundStyle(LifeTheme.coral).symbolSize(24)
                }
                if let selected {
                    RuleMark(x: .value("Selected", selected.at)).foregroundStyle(LifeTheme.muted.opacity(0.5))
                        .lineStyle(StrokeStyle(lineWidth: 1, dash: [3, 4]))
                    PointMark(x: .value("Time", selected.at), y: .value("Heart rate", selected.bpm))
                        .foregroundStyle(LifeTheme.mint).symbolSize(45)
                }
            }
            .chartXScale(domain: start...now).chartYScale(domain: extent)
            .chartYAxis { AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5, dash: [3, 5])).foregroundStyle(.white.opacity(0.09))
                AxisValueLabel().foregroundStyle(LifeTheme.muted).font(.system(size: 9))
            } }
            .chartXAxis { AxisMarks(values: [start, start.addingTimeInterval(Double(minutes * 30)), now]) { _ in
                AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(LifeTheme.muted).font(.system(size: 9))
            } }
            .chartOverlay { proxy in
                GeometryReader { geometry in
                    Rectangle().fill(.clear).contentShape(Rectangle())
                        .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                            let x = value.location.x - geometry[proxy.plotAreaFrame].origin.x
                            selectedAt = proxy.value(atX: x, as: Date.self)
                        }.onEnded { _ in selectedAt = nil })
                }
            }
            .frame(height: 145)
            .accessibilityLabel("Heart rate over the last \(minutes) minutes")
        }
    }

    private func statistic(_ label: String, value: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(label).font(.system(size: 9, weight: .medium)).tracking(1.3).foregroundStyle(LifeTheme.muted)
            Text("\(value)").font(.system(size: 21, weight: .regular, design: .rounded)).monospacedDigit()
        }.accessibilityElement(children: .ignore).accessibilityLabel("\(label) \(value) beats per minute")
    }
}
