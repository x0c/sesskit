//! Usage observations, reconciliation and the `known_total` total.
//!
//! Mirrors `src/sesskit/usage.py` (`_record_key`, `reconcile`, `summarize`
//! `build_total`) for the total-token metric. Session rollups never enter the
//! sum, so they are not modelled here. Every record keeps the native record
//! time of the observation(s) it came from.

use std::collections::HashMap;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Scope {
    Call,
    Message,
    Turn,
    Session,
    Unknown,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Counter {
    Absolute,
    Cumulative,
    Unknown,
}

/// Normalized token metrics (`NormalizedUsage`); cost participates in equality.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Normalized {
    pub input_total: Option<i64>,
    pub input_uncached: Option<i64>,
    pub cache_read: Option<i64>,
    pub cache_write: Option<i64>,
    pub output_total: Option<i64>,
    pub reasoning: Option<i64>,
    pub total: Option<i64>,
    pub reported_total: Option<i64>,
    pub cost: Option<f64>,
}

impl Normalized {
    /// Calculated total preferred; native reported total as fallback.
    pub fn effective_total(&self) -> Option<i64> {
        self.total.or(self.reported_total)
    }

    /// Anthropic/Pi/Kimi style: input buckets are inclusive parts of input.
    pub fn inclusive(
        input: Option<i64>,
        cache_read: Option<i64>,
        cache_write: Option<i64>,
        output: Option<i64>,
        reasoning: Option<i64>,
        reported_total: Option<i64>,
        cost: Option<f64>,
    ) -> Self {
        let parts = [input, cache_read, cache_write];
        let present: Vec<i64> = parts.iter().flatten().copied().collect();
        let input_total = if present.is_empty() { None } else { Some(present.iter().sum()) };
        let total = match (input_total, output) {
            (Some(i), Some(o)) => Some(i + o),
            _ => None,
        };
        Normalized {
            input_total,
            input_uncached: input,
            cache_read,
            cache_write,
            output_total: output,
            reasoning,
            total,
            reported_total,
            cost,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Observation {
    pub record: String,
    pub scope: Scope,
    pub counter: Counter,
    pub response_id: Option<String>,
    pub turn_id: Option<String>,
    pub message_id: Option<String>,
    pub scope_id: Option<String>,
    pub counter_name: Option<String>,
    pub model: Option<String>,
    pub normalized: Normalized,
    /// Native record time (epoch seconds) of the row carrying this usage.
    pub ts: Option<f64>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Standing {
    Firm,
    Ambiguous,
    Conflicted,
}

#[derive(Clone, Debug)]
pub struct Record {
    pub standing: Standing,
    pub scope: Scope,
    pub counter: Counter,
    pub turn_id: Option<String>,
    pub counter_name: Option<String>,
    pub normalized: Normalized,
    pub ts: Option<f64>,
    /// Stable position, used where the reference keys groups by usage id.
    pub index: usize,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum Key {
    Response(String, Scope, Counter),
    Counter(String, String, Scope),
    SessionCounter(String, String, Option<String>),
    Scope(Scope, String),
    Record(usize),
}

fn record_key(obs: &Observation, position: usize) -> Key {
    if let Some(rid) = &obs.response_id {
        return Key::Response(rid.clone(), obs.scope, obs.counter);
    }
    if obs.counter == Counter::Cumulative {
        if let Some(turn) = &obs.turn_id {
            return Key::Counter(turn.clone(), obs.counter_name.clone().unwrap_or_default(), obs.scope);
        }
        if obs.scope == Scope::Session {
            if let Some(sid) = &obs.scope_id {
                return Key::SessionCounter(
                    sid.clone(),
                    obs.counter_name.clone().unwrap_or_default(),
                    obs.model.clone(),
                );
            }
        }
    }
    if let Some(sid) = &obs.scope_id {
        return Key::Scope(obs.scope, sid.clone());
    }
    Key::Record(position)
}

/// Collapse proven duplicates, fold cumulative revisions, keep the rest.
pub fn reconcile(observations: &[Observation]) -> Vec<Record> {
    let mut order: Vec<Key> = Vec::new();
    let mut groups: HashMap<Key, Vec<&Observation>> = HashMap::new();
    for (i, obs) in observations.iter().enumerate() {
        let key = record_key(obs, i);
        let entry = groups.entry(key.clone()).or_default();
        if entry.is_empty() {
            order.push(key);
        }
        entry.push(obs);
    }
    let mut records = Vec::new();
    for key in order {
        let members = &groups[&key];
        let first = members[0];
        let push = |records: &mut Vec<Record>, standing, src: &Observation, normalized: Normalized, ts| {
            let index = records.len();
            records.push(Record {
                standing,
                scope: first.scope,
                counter: first.counter,
                turn_id: src.turn_id.clone(),
                counter_name: first.counter_name.clone(),
                normalized,
                ts,
                index,
            });
        };
        if members.len() == 1 {
            let standing = if matches!(key, Key::Record(_)) { Standing::Ambiguous } else { Standing::Firm };
            push(&mut records, standing, first, first.normalized.clone(), first.ts);
        } else if members.iter().skip(1).all(|m| m.normalized == first.normalized) {
            push(&mut records, Standing::Firm, first, first.normalized.clone(), first.ts);
        } else if matches!(key, Key::Counter(..) | Key::SessionCounter(..)) {
            let latest = members[members.len() - 1];
            // Turn id of a folded counter is the group's first member's.
            push(&mut records, Standing::Firm, first, latest.normalized.clone(), latest.ts);
        } else {
            for member in members {
                let index = records.len();
                records.push(Record {
                    standing: Standing::Conflicted,
                    scope: first.scope,
                    counter: first.counter,
                    turn_id: member.turn_id.clone(),
                    counter_name: first.counter_name.clone(),
                    normalized: member.normalized.clone(),
                    ts: member.ts,
                    index,
                });
            }
        }
    }
    records
}

struct GroupView<'a> {
    details: Vec<&'a Record>,
    interval: Option<&'a Record>,
    unresolved: bool,
}

/// One contributing amount with the native time it is attributed to.
#[derive(Clone, Copy, Debug)]
pub struct Contribution {
    pub tokens: i64,
    pub ts: Option<f64>,
}

/// `known_total` of total tokens over `records`, plus its time-attributed parts.
pub fn known_total(records: &[&Record]) -> (Option<i64>, Vec<Contribution>) {
    let firm: Vec<&Record> = records.iter().copied().filter(|r| r.standing == Standing::Firm).collect();
    let is_detail = |r: &Record| {
        r.counter == Counter::Absolute && matches!(r.scope, Scope::Call | Scope::Message | Scope::Turn)
    };
    let details: Vec<&Record> = firm.iter().copied().filter(|r| is_detail(r)).collect();
    let turn_cumulatives: Vec<&Record> = firm
        .iter()
        .copied()
        .filter(|r| !is_detail(r) && r.scope != Scope::Session && r.counter == Counter::Cumulative)
        .collect();

    let mut order: Vec<String> = Vec::new();
    let mut groups: HashMap<String, Vec<&Record>> = HashMap::new();
    for r in details.iter().chain(turn_cumulatives.iter()) {
        let key = r.turn_id.clone().unwrap_or_else(|| format!("@record:{}", r.index));
        let entry = groups.entry(key.clone()).or_default();
        if entry.is_empty() {
            order.push(key);
        }
        entry.push(r);
    }

    let mut views: Vec<GroupView> = Vec::new();
    for key in &order {
        let members = &groups[key];
        let fine: Vec<&Record> = members
            .iter()
            .copied()
            .filter(|r| r.counter == Counter::Absolute && matches!(r.scope, Scope::Call | Scope::Message))
            .collect();
        let mut turn_totals: Vec<&Record> = members
            .iter()
            .copied()
            .filter(|r| r.counter == Counter::Absolute && r.scope == Scope::Turn)
            .collect();
        if !turn_totals.is_empty() && !fine.is_empty() {
            turn_totals.clear();
        }
        let member_details: Vec<&Record> = fine.iter().chain(turn_totals.iter()).copied().collect();
        let intervals: Vec<&Record> = members
            .iter()
            .copied()
            .filter(|r| !member_details.iter().any(|d| std::ptr::eq(*d, *r)))
            .collect();
        if intervals.is_empty() {
            views.push(GroupView { details: member_details, interval: None, unresolved: false });
            continue;
        }
        let mut counters: Vec<(Option<String>, Option<String>, Scope)> = Vec::new();
        for r in &intervals {
            let k = (r.turn_id.clone(), r.counter_name.clone(), r.scope);
            if !counters.contains(&k) {
                counters.push(k);
            }
        }
        if counters.len() > 1 {
            views.push(GroupView { details: member_details, interval: None, unresolved: true });
            continue;
        }
        views.push(GroupView { details: member_details, interval: intervals.last().copied(), unresolved: false });
    }

    let mut detail_total: Option<i64> = None;
    let mut interval_total: Option<i64> = None;
    let mut parts: Vec<Contribution> = Vec::new();
    for view in &views {
        if view.unresolved {
            continue;
        }
        let detail_vals: Vec<(i64, Option<f64>)> = view
            .details
            .iter()
            .filter_map(|r| r.normalized.effective_total().map(|v| (v, r.ts)))
            .collect();
        let group_detail: Option<i64> =
            if detail_vals.is_empty() { None } else { Some(detail_vals.iter().map(|(v, _)| v).sum()) };
        let add_details = |detail_total: &mut Option<i64>, parts: &mut Vec<Contribution>| {
            if let Some(g) = group_detail {
                *detail_total = Some(detail_total.unwrap_or(0) + g);
                parts.extend(detail_vals.iter().map(|(v, ts)| Contribution { tokens: *v, ts: *ts }));
            }
        };
        let Some(interval) = view.interval else {
            add_details(&mut detail_total, &mut parts);
            continue;
        };
        let Some(group_interval) = interval.normalized.effective_total() else {
            add_details(&mut detail_total, &mut parts);
            continue;
        };
        if let Some(g) = group_detail {
            if g > group_interval {
                continue;
            }
        }
        let residual = group_interval - group_detail.unwrap_or(0);
        interval_total = Some(interval_total.unwrap_or(0) + residual);
        parts.push(Contribution { tokens: residual, ts: interval.ts });
        if group_detail.is_some() {
            add_details(&mut detail_total, &mut parts);
        }
    }
    let known = if detail_total.is_none() && interval_total.is_none() {
        None
    } else {
        Some(detail_total.unwrap_or(0) + interval_total.unwrap_or(0))
    };
    (known, parts)
}

/// Python `_int`: non-negative integers (integral floats accepted), bools rejected.
pub fn int_of(value: Option<&serde_json::Value>) -> Option<i64> {
    match value? {
        serde_json::Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                return i64::try_from(u).ok();
            }
            if let Some(i) = n.as_i64() {
                return (i >= 0).then_some(i);
            }
            let f = n.as_f64()?;
            (f.is_finite() && f >= 0.0 && f.fract() == 0.0 && f < 9.2e18).then_some(f as i64)
        }
        _ => None,
    }
}

/// Non-negative numeric cost (`costUSD`, Pi `cost.total`, OpenCode `cost`).
pub fn cost_of(value: Option<&serde_json::Value>) -> Option<f64> {
    match value? {
        serde_json::Value::Number(n) => n.as_f64().filter(|f| *f >= 0.0),
        _ => None,
    }
}

pub fn str_of(value: Option<&serde_json::Value>) -> Option<String> {
    match value? {
        serde_json::Value::String(s) if !s.is_empty() => Some(s.clone()),
        _ => None,
    }
}

pub fn trimmed(value: Option<&serde_json::Value>) -> Option<String> {
    match value? {
        serde_json::Value::String(s) => {
            let t = s.trim();
            (!t.is_empty()).then(|| t.to_string())
        }
        _ => None,
    }
}
