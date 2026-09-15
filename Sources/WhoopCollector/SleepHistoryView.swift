import Foundation
import SwiftUI

extension SleepSession {
    var durationSeconds: Double? { end.map { $0 - start - awake_seconds } }
}

private func sleepDuration(_ seconds: Double) -> String {
    let minutes = Int(max(0, seconds) / 60)
    return minutes >= 60 ? "\(minutes / 60)h \(minutes % 60)m" : "\(minutes)m"
}

/// Unsaved form state. New entries have no assumed bedtime or waking time.
struct SleepDraft {
    private let original: SleepSession?
    private let identifier: String
    var start: Date?
    var end: Date?
    var finished: Bool
    var awakeMinutes: String
    static let earliestDate = Date(timeIntervalSince1970: 1_577_836_800)

    init(row: SleepSession? = nil) {
        original = row; identifier = row?.id ?? UUID().uuidString.lowercased()
        start = row.map { Date(timeIntervalSince1970: $0.start) }
        end = row?.end.map { Date(timeIntervalSince1970: $0) }
        finished = row == nil || row?.end != nil
        awakeMinutes = String(format: "%g", (row?.awake_seconds ?? 0) / 60)
    }
    var awakeSeconds: Double? {
        guard finished else { return 0 }
        // Accept the decimal separator used by the phone's locale.
        let text = awakeMinutes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let formatter = NumberFormatter(); formatter.numberStyle = .decimal
        let separator = formatter.decimalSeparator ?? "."
        let normalized = text.replacingOccurrences(of: separator, with: ".")
        return Double(normalized).flatMap { $0.isFinite ? $0 * 60 : nil }
    }
    var duration: Double? {
        guard finished, let start, let end, let awakeSeconds else { return nil }
        return end.timeIntervalSince(start) - awakeSeconds
    }
    func validationMessage(now: Date, sessions: [SleepSession]) -> String? {
        guard let start else { return "Choose a bedtime." }
        guard start >= Self.earliestDate, start <= now else { return "Bedtime must be in the past." }
        if finished {
            guard let end else { return "Choose a wake-up time." }
            guard end <= now else { return "Wake-up time must be in the past." }
            guard end > start else { return "Wake-up time must be after bedtime. Check both dates." }
            guard end.timeIntervalSince(start) <= 86_400 else { return "One sleep entry can span up to 24 hours." }
            guard let awakeSeconds, awakeSeconds >= 0 else { return "Enter zero or more minutes awake." }
            guard awakeSeconds < end.timeIntervalSince(start) else { return "Awake time must be shorter than the sleep interval." }
        }
        let finish = finished ? end!.timeIntervalSince1970 : Double.infinity
        if let overlap = sessions.first(where: {
            $0.id != identifier && $0.start < finish && ($0.end ?? .infinity) > start.timeIntervalSince1970
        }) {
            let date = Date(timeIntervalSince1970: overlap.start).formatted(date: .abbreviated, time: .shortened)
            return "Overlaps the entry starting \(date). Edit that entry in Sleep history."
        }
        if let original, let current = sessions.first(where: { $0.id == identifier }), current.revision != original.revision {
            return "This entry changed while you were editing. Reopen it to use the latest version."
        }
        return nil
    }
    func session(now: Date, sessions: [SleepSession]) -> SleepSession? {
        guard validationMessage(now: now, sessions: sessions) == nil, let start else { return nil }
        return SleepSession(id: identifier, revision: (original?.revision ?? 0) + 1,
                            start: start.timeIntervalSince1970, end: finished ? end?.timeIntervalSince1970 : nil,
                            awake_seconds: awakeSeconds ?? 0, updated_at: original?.updated_at)
    }
    var hasChanges: Bool {
        guard let original else { return true }
        return start?.timeIntervalSince1970 != original.start || (finished ? end?.timeIntervalSince1970 : nil) != original.end || awakeSeconds != original.awake_seconds
    }
}

struct SleepHistoryView: View {
    @ObservedObject var client: WellnessClient
    @Environment(\.dismiss) private var dismiss
    @State private var adding = false
    @State private var editing: SleepSession?
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Button { adding = true } label: { Label("Log past sleep", systemImage: "plus.circle.fill") }
                        .accessibilityIdentifier("sleep-history-add")
                } footer: {
                    Text("Enter a missed night or tap an entry to correct it. Times use \(TimeZone.current.identifier).")
                }
                if client.journal.sessions.isEmpty {
                    Section {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("No sleep logged yet", systemImage: "moon.stars").font(.headline)
                            Text("You can add previous nights without starting a live sleep session.")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }.padding(.vertical, 12)
                    }
                } else {
                    Section("Your entries") {
                        ForEach(client.journal.sessions) { row in
                            Button { editing = row } label: { historyRow(row) }
                                .buttonStyle(.plain)
                                .accessibilityHint("Edit bedtime, waking and minutes awake")
                        }
                    }
                }
                Section {
                    if let message = client.message { Text(message).font(.caption).foregroundStyle(LifeTheme.coral) }
                    Button { Task { await client.refresh() } } label: { Label("Sync now", systemImage: "arrow.trianglehead.2.clockwise.rotate.90") }
                } footer: {
                    Text("Changes are saved on this phone first. Pending entries sync when the backend is available.")
                }
            }
            .scrollContentBackground(.hidden).background(LifeTheme.canvas)
            .navigationTitle("Sleep history")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .refreshable { await client.refresh() }
            .task { await client.refresh() }
            .sheet(isPresented: $adding) { SleepEditor(client: client) }
            .sheet(item: $editing) { SleepEditor(client: client, row: $0) }
        }.tint(LifeTheme.mint).preferredColorScheme(.dark)
    }
    private func historyRow(_ row: SleepSession) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(Date(timeIntervalSince1970: row.end ?? row.start), format: .dateTime.weekday(.wide).month(.abbreviated).day().year())
                    .font(.subheadline.weight(.semibold))
                Spacer()
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(LifeTheme.muted)
            }
            HStack(alignment: .firstTextBaseline) {
                Text(row.durationSeconds.map(sleepDuration) ?? "In progress")
                    .font(.system(size: 24, weight: .medium, design: .rounded)).monospacedDigit()
                Spacer()
                Text(client.journal.pending.contains(row.id) ? "On phone · pending" : "Synced")
                    .font(.caption).foregroundStyle(client.journal.pending.contains(row.id) ? LifeTheme.coral : LifeTheme.muted)
            }
            Text(Date(timeIntervalSince1970: row.start).formatted(date: .abbreviated, time: .shortened) + " → " +
                 (row.end.map { Date(timeIntervalSince1970: $0).formatted(date: .abbreviated, time: .shortened) } ?? "Still sleeping"))
                .font(.caption).foregroundStyle(LifeTheme.muted)
            if row.awake_seconds > 0 { Text("\(sleepDuration(row.awake_seconds)) awake subtracted").font(.caption2).foregroundStyle(LifeTheme.muted) }
        }.foregroundStyle(LifeTheme.text).padding(.vertical, 7).accessibilityElement(children: .combine)
    }
}

struct SleepEditor: View {
    @ObservedObject var client: WellnessClient
    private let original: SleepSession?
    @Environment(\.dismiss) private var dismiss
    @State private var draft: SleepDraft
    @State private var picker: SleepTimeField?
    @State private var saveError: String?
    init(client: WellnessClient, row: SleepSession? = nil) {
        self.client = client; original = row; _draft = State(initialValue: SleepDraft(row: row))
    }
    private var validation: String? { draft.validationMessage(now: .now, sessions: client.journal.sessions) }
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("SLEEP DURATION").font(.caption.weight(.semibold)).tracking(1).foregroundStyle(LifeTheme.mint)
                        Text(draft.duration.flatMap { $0 > 0 && validation == nil ? sleepDuration($0) : nil } ?? "—")
                            .font(.system(size: 36, weight: .regular, design: .rounded)).monospacedDigit()
                        Text("Time in bed minus minutes awake").font(.caption).foregroundStyle(.secondary)
                    }.padding(.vertical, 8)
                }
                Section {
                    timeButton(.bedtime, value: draft.start)
                    if original?.end == nil && original != nil { Toggle("Finished sleeping", isOn: $draft.finished) }
                    if draft.finished { timeButton(.waking, value: draft.end) }
                } header: { Text("Dates & times") } footer: {
                    Text("Include the correct date for each time. Overnight sleep usually starts the previous day. \(TimeZone.current.identifier).")
                }
                if draft.finished {
                    Section {
                        HStack {
                            Text("Minutes awake")
                            Spacer()
                            TextField("0", text: $draft.awakeMinutes)
                                .multilineTextAlignment(.trailing).frame(maxWidth: 90)
                                .accessibilityLabel("Minutes awake")
                                #if os(iOS)
                                .keyboardType(.decimalPad)
                                #endif
                        }
                    } footer: { Text("Include time taken to fall asleep and time awake during the night.") }
                }
                if let validation { Section { Text(validation).font(.footnote).foregroundStyle(.secondary) } }
                if let saveError { Section { Text(saveError).foregroundStyle(LifeTheme.coral) } }
                Section {
                    Button(original == nil ? "Save sleep" : "Save changes", action: save)
                        .frame(maxWidth: .infinity).fontWeight(.semibold)
                        .disabled(validation != nil || !draft.hasChanges)
                        .accessibilityIdentifier("sleep-entry-save")
                } footer: { Text("Self-reported. Saved locally, then synced to your backend.") }
            }
            .scrollContentBackground(.hidden).background(LifeTheme.canvas)
            .navigationTitle(original == nil ? "Log past sleep" : "Edit sleep")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save", action: save).disabled(validation != nil || !draft.hasChanges)
                }
            }
            .sheet(item: $picker) { field in
                SleepTimePicker(field: field, selected: field == .bedtime ? draft.start : draft.end,
                                seed: field == .bedtime ? (draft.end ?? .now) : (draft.start ?? .now)) { value in
                    if field == .bedtime { draft.start = value } else { draft.end = value }
                    saveError = nil
                }
            }
        }.tint(LifeTheme.mint).preferredColorScheme(.dark)
    }
    private func timeButton(_ field: SleepTimeField, value: Date?) -> some View {
        Button { picker = field } label: {
            HStack(alignment: .center) {
                Label(field.title, systemImage: field == .bedtime ? "moon" : "sunrise")
                    .foregroundStyle(LifeTheme.text)
                Spacer()
                VStack(alignment: .trailing, spacing: 3) {
                    if let value {
                        Text(value, format: .dateTime.hour().minute()).fontWeight(.medium)
                        Text(value, format: .dateTime.weekday(.abbreviated).month(.abbreviated).day().year()).font(.caption).foregroundStyle(LifeTheme.muted)
                    } else { Text("Choose date & time").font(.subheadline) }
                }
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(LifeTheme.muted)
            }.padding(.vertical, 5)
        }.accessibilityIdentifier(field == .bedtime ? "sleep-bedtime" : "sleep-waking")
    }
    private func save() {
        guard let row = draft.session(now: .now, sessions: client.journal.sessions) else { return }
        if client.saveSleep(row) { dismiss() } else { saveError = client.message }
    }
}

private enum SleepTimeField: String, Identifiable {
    case bedtime, waking
    var id: String { rawValue }
    var title: String { self == .bedtime ? "Bedtime" : "Woke up" }
}
private struct SleepTimePicker: View {
    let field: SleepTimeField
    let onSelect: (Date) -> Void
    @State private var value: Date
    @Environment(\.dismiss) private var dismiss
    init(field: SleepTimeField, selected: Date?, seed: Date, onSelect: @escaping (Date) -> Void) {
        self.field = field; self.onSelect = onSelect
        // Picker seed is only committed after the user taps Use time.
        _value = State(initialValue: selected ?? seed)
    }
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DatePicker(field.title, selection: $value, in: SleepDraft.earliestDate...Date.now,
                               displayedComponents: [.date, .hourAndMinute])
                        #if os(iOS)
                        .datePickerStyle(.graphical)
                        #endif
                }
                Section {
                    Text(value, format: .dateTime.weekday(.wide).month(.wide).day().year().hour().minute())
                        .font(.subheadline)
                    Button("Use time") { onSelect(Calendar.current.dateInterval(of: .minute, for: value)?.start ?? value); dismiss() }.frame(maxWidth: .infinity)
                }
            }.scrollContentBackground(.hidden).background(LifeTheme.canvas)
                .navigationTitle(field.title)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }.tint(LifeTheme.mint).preferredColorScheme(.dark)
    }
}
