from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from itertools import combinations
from uuid import NAMESPACE_URL, uuid5

from .types import (
    BankTransaction,
    MatchGroup,
    PayrollExpected,
    ReconPolicy,
    ReconResult,
    RunSummary,
    Variance,
    q2,
)


def _stable_group_id(run_id: str, kind: str, expected_ids: list[str], bank_ids: list[str]) -> str:
    key = f"{run_id}:{kind}:{','.join(sorted(expected_ids))}:{','.join(sorted(bank_ids))}"
    return str(uuid5(NAMESPACE_URL, key))


def _stable_variance_id(run_id: str, code: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{run_id}:{code}:{key}"))


def _within_window(left: date, right: date, days: int) -> tuple[bool, int]:
    distance = abs((left - right).days)
    return distance <= days, distance


def _account_allowed(policy: ReconPolicy, allowed_accounts: set[str], account_ref: str | None) -> bool:
    if not policy.require_allowed_account:
        return True
    if not allowed_accounts:
        return False
    return (account_ref or "") in allowed_accounts


def _combo_scores_one_to_many(
    expected: PayrollExpected,
    combo: tuple[BankTransaction, ...],
) -> tuple[Decimal, int, str]:
    bank_total = q2(sum((bank.withdrawal_abs for bank in combo), start=Decimal("0")))
    delta = q2(abs(bank_total - expected.net_amount))
    max_distance = max(abs((bank.posted_date - expected.payment_date).days) for bank in combo)
    lex = ",".join(sorted(bank.id for bank in combo))
    return delta, max_distance, lex


def _combo_scores_many_to_one(
    bank: BankTransaction,
    combo: tuple[PayrollExpected, ...],
) -> tuple[Decimal, int, str]:
    expected_total = q2(sum((item.net_amount for item in combo), start=Decimal("0")))
    delta = q2(abs(expected_total - bank.withdrawal_abs))
    max_distance = max(abs((bank.posted_date - item.payment_date).days) for item in combo)
    lex = ",".join(sorted(item.id for item in combo))
    return delta, max_distance, lex


def _make_group(
    run_id: str,
    kind: str,
    expected_items: list[PayrollExpected],
    bank_items: list[BankTransaction],
) -> MatchGroup:
    expected_ids = sorted(item.id for item in expected_items)
    bank_ids = sorted(item.id for item in bank_items)
    expected_total = q2(sum((item.net_amount for item in expected_items), start=Decimal("0")))
    bank_total = q2(sum((item.withdrawal_abs for item in bank_items), start=Decimal("0")))
    matched_on = max((item.posted_date for item in bank_items), default=None)
    return MatchGroup(
        id=_stable_group_id(run_id, kind, expected_ids, bank_ids),
        group_kind=kind,
        match_confidence="deterministic",
        expected_ids=expected_ids,
        bank_ids=bank_ids,
        expected_total=expected_total,
        bank_total=bank_total,
        delta=q2(expected_total - bank_total),
        matched_on=matched_on,
    )


def _make_amb_variance(
    run_id: str,
    key: str,
    message: str,
    details: dict,
    event_date: date | None = None,
) -> Variance:
    return Variance(
        id=_stable_variance_id(run_id, "AMB-001", key),
        code="AMB-001",
        category="bank",
        severity="review",
        status="open",
        message=message,
        event_date=event_date,
        details=details,
    )


def reconcile_bank(
    run_id: str,
    expected_items: list[PayrollExpected],
    bank_items: list[BankTransaction],
    policy: ReconPolicy,
    allowed_accounts: set[str] | None = None,
) -> ReconResult:
    allowed_accounts = allowed_accounts or set()
    expected_by_id = {item.id: item for item in expected_items}
    bank_by_id = {item.id: item for item in bank_items}

    ordered_expected = sorted(expected_items, key=lambda item: item.id)
    ordered_bank = sorted(bank_items, key=lambda item: item.id)

    unmatched_expected = {item.id for item in ordered_expected}
    unmatched_bank = {item.id for item in ordered_bank}
    ambiguous_expected: set[str] = set()

    groups: list[MatchGroup] = []
    variances: list[Variance] = []

    # Pass A: one-to-one exact matches.
    for expected in ordered_expected:
        if expected.id not in unmatched_expected:
            continue
        candidates: list[tuple[BankTransaction, int]] = []
        for bank in ordered_bank:
            if bank.id not in unmatched_bank:
                continue
            if not _account_allowed(policy, allowed_accounts, bank.account_ref):
                continue
            in_window, distance = _within_window(expected.payment_date, bank.posted_date, policy.date_window_days)
            if not in_window:
                continue
            if q2(abs(bank.withdrawal_abs - expected.net_amount)) <= q2(policy.amount_tolerance):
                candidates.append((bank, distance))

        if not candidates:
            continue
        score_groups = defaultdict(list)
        for bank, distance in candidates:
            score_groups[(q2(abs(bank.withdrawal_abs - expected.net_amount)), distance)].append(bank)
        best_score = sorted(score_groups.keys())[0]
        best_candidates = sorted(score_groups[best_score], key=lambda item: item.id)
        if len(best_candidates) > 1:
            variances.append(
                _make_amb_variance(
                    run_id,
                    f"exp:{expected.id}:one_to_one",
                    "Ambiguous one-to-one bank match candidates",
                    {
                        "expected_id": expected.id,
                        "candidate_bank_ids": [item.id for item in best_candidates],
                    },
                    expected.payment_date,
                )
            )
            ambiguous_expected.add(expected.id)
            continue

        bank = best_candidates[0]
        group = _make_group(run_id, "one_to_one", [expected], [bank])
        groups.append(group)
        unmatched_expected.remove(expected.id)
        unmatched_bank.remove(bank.id)

    # Pass B: one expected to many bank rows.
    if policy.enable_one_to_many:
        for expected in ordered_expected:
            if expected.id not in unmatched_expected or expected.id in ambiguous_expected:
                continue
            candidate_bank = []
            for bank_id in sorted(unmatched_bank):
                bank = bank_by_id[bank_id]
                if not _account_allowed(policy, allowed_accounts, bank.account_ref):
                    continue
                in_window, _ = _within_window(expected.payment_date, bank.posted_date, policy.date_window_days)
                if in_window:
                    candidate_bank.append(bank)

            scored: list[tuple[tuple[Decimal, int, str], tuple[BankTransaction, ...]]] = []
            max_size = min(policy.max_group_size, len(candidate_bank))
            for size in range(2, max_size + 1):
                for combo in combinations(candidate_bank, size):
                    if q2(abs(q2(sum((item.withdrawal_abs for item in combo), start=Decimal("0"))) - expected.net_amount)) <= q2(policy.amount_tolerance):
                        scored.append((_combo_scores_one_to_many(expected, combo), combo))

            if not scored:
                continue
            scored.sort(key=lambda item: item[0])
            best_score = scored[0][0]
            best = [combo for score, combo in scored if (score[0], score[1]) == (best_score[0], best_score[1])]
            if len(best) > 1:
                details = {
                    "expected_id": expected.id,
                    "candidate_bank_groups": [sorted(item.id for item in combo) for combo in best],
                }
                variances.append(
                    _make_amb_variance(
                        run_id,
                        f"exp:{expected.id}:one_to_many",
                        "Ambiguous one-to-many bank group candidates",
                        details,
                        expected.payment_date,
                    )
                )
                ambiguous_expected.add(expected.id)
                continue

            chosen_combo = sorted(best[0], key=lambda item: item.id)
            group = _make_group(run_id, "one_to_many", [expected], chosen_combo)
            groups.append(group)
            unmatched_expected.remove(expected.id)
            for bank in chosen_combo:
                unmatched_bank.remove(bank.id)

    # Pass C: many expected to one bank row.
    if policy.enable_many_to_one:
        for bank_id in sorted(unmatched_bank):
            bank = bank_by_id[bank_id]
            if not _account_allowed(policy, allowed_accounts, bank.account_ref):
                continue
            candidate_expected = []
            for expected_id in sorted(unmatched_expected):
                if expected_id in ambiguous_expected:
                    continue
                expected = expected_by_id[expected_id]
                in_window, _ = _within_window(expected.payment_date, bank.posted_date, policy.date_window_days)
                if in_window:
                    candidate_expected.append(expected)

            scored: list[tuple[tuple[Decimal, int, str], tuple[PayrollExpected, ...]]] = []
            max_size = min(policy.max_group_size, len(candidate_expected))
            for size in range(2, max_size + 1):
                for combo in combinations(candidate_expected, size):
                    if q2(abs(q2(sum((item.net_amount for item in combo), start=Decimal("0"))) - bank.withdrawal_abs)) <= q2(policy.amount_tolerance):
                        scored.append((_combo_scores_many_to_one(bank, combo), combo))

            if not scored:
                continue
            scored.sort(key=lambda item: item[0])
            best_score = scored[0][0]
            best = [combo for score, combo in scored if (score[0], score[1]) == (best_score[0], best_score[1])]
            if len(best) > 1:
                details = {
                    "bank_id": bank.id,
                    "candidate_expected_groups": [sorted(item.id for item in combo) for combo in best],
                }
                variances.append(
                    _make_amb_variance(
                        run_id,
                        f"bank:{bank.id}:many_to_one",
                        "Ambiguous many-to-one expected group candidates",
                        details,
                        bank.posted_date,
                    )
                )
                continue

            chosen_combo = sorted(best[0], key=lambda item: item.id)
            group = _make_group(run_id, "many_to_one", chosen_combo, [bank])
            groups.append(group)
            unmatched_bank.remove(bank.id)
            for expected in chosen_combo:
                unmatched_expected.remove(expected.id)

    # Duplicate withdrawals BNK-011.
    dup_groups: dict[tuple[str, date, str], list[BankTransaction]] = defaultdict(list)
    for bank in ordered_bank:
        key = (bank.account_ref or "", bank.posted_date, f"{bank.withdrawal_abs:.2f}")
        dup_groups[key].append(bank)
    for (account_ref, event_date, abs_amount), members in sorted(dup_groups.items(), key=lambda item: (item[0][0], item[0][1], item[0][2])):
        if len(members) < 2:
            continue
        variances.append(
            Variance(
                id=_stable_variance_id(run_id, "BNK-011", f"{account_ref}:{event_date}:{abs_amount}"),
                code="BNK-011",
                category="bank",
                severity="blocker",
                status="open",
                message="Duplicate withdrawals detected",
                amount=Decimal(abs_amount),
                event_date=event_date,
                account_ref=account_ref or None,
                details={"bank_transaction_ids": [item.id for item in sorted(members, key=lambda item: item.id)]},
            )
        )

    # Remaining expected-side variance classification.
    for expected_id in sorted(unmatched_expected):
        if expected_id in ambiguous_expected:
            continue
        expected = expected_by_id[expected_id]
        within_window_any_amount = []
        within_window_same_amount_any_account = []
        within_window_same_amount_allowed = []
        anydate_same_amount_allowed = []

        for bank in ordered_bank:
            in_window, date_distance = _within_window(expected.payment_date, bank.posted_date, policy.date_window_days)
            amount_matches = q2(abs(bank.withdrawal_abs - expected.net_amount)) <= q2(policy.amount_tolerance)
            allowed = _account_allowed(policy, allowed_accounts, bank.account_ref)
            if in_window:
                if allowed:
                    within_window_any_amount.append((bank, q2(abs(bank.withdrawal_abs - expected.net_amount)), date_distance))
                if amount_matches:
                    within_window_same_amount_any_account.append((bank, date_distance))
                    if allowed:
                        within_window_same_amount_allowed.append((bank, date_distance))
            if amount_matches and allowed:
                anydate_same_amount_allowed.append((bank, date_distance))

        if policy.require_allowed_account and within_window_same_amount_any_account and not within_window_same_amount_allowed:
            chosen = sorted(within_window_same_amount_any_account, key=lambda item: (item[1], item[0].id))[0][0]
            variances.append(
                Variance(
                    id=_stable_variance_id(run_id, "BNK-031", expected.id),
                    code="BNK-031",
                    category="bank",
                    severity="blocker",
                    status="open",
                    message="Withdrawal was paid from an unexpected account",
                    amount=expected.net_amount,
                    event_date=chosen.posted_date,
                    account_ref=chosen.account_ref,
                    details={"expected_id": expected.id, "bank_id": chosen.id},
                )
            )
            continue

        if anydate_same_amount_allowed and not within_window_same_amount_allowed:
            chosen = sorted(anydate_same_amount_allowed, key=lambda item: (item[1], item[0].id))[0][0]
            variances.append(
                Variance(
                    id=_stable_variance_id(run_id, "BNK-021", expected.id),
                    code="BNK-021",
                    category="bank",
                    severity="blocker",
                    status="open",
                    message="Matching withdrawal exists outside configured date window",
                    amount=expected.net_amount,
                    event_date=chosen.posted_date,
                    account_ref=chosen.account_ref,
                    details={"expected_id": expected.id, "bank_id": chosen.id},
                )
            )
            continue

        if within_window_any_amount:
            chosen_bank, amount_delta, _ = sorted(within_window_any_amount, key=lambda item: (item[1], item[2], item[0].id))[0]
            variances.append(
                Variance(
                    id=_stable_variance_id(run_id, "BNK-002", expected.id),
                    code="BNK-002",
                    category="bank",
                    severity="blocker",
                    status="open",
                    message="Withdrawal amount mismatch",
                    amount=q2(abs(chosen_bank.withdrawal_abs - expected.net_amount)),
                    event_date=chosen_bank.posted_date,
                    account_ref=chosen_bank.account_ref,
                    details={
                        "expected_id": expected.id,
                        "bank_id": chosen_bank.id,
                        "expected_amount": f"{expected.net_amount:.2f}",
                        "bank_amount": f"{chosen_bank.withdrawal_abs:.2f}",
                    },
                )
            )
            continue

        variances.append(
            Variance(
                id=_stable_variance_id(run_id, "BNK-001", expected.id),
                code="BNK-001",
                category="bank",
                severity="blocker",
                status="open",
                message="Expected withdrawal is missing",
                amount=expected.net_amount,
                event_date=expected.payment_date,
                details={"expected_id": expected.id},
            )
        )

    groups.sort(key=lambda group: (group.group_kind, ",".join(group.expected_ids), ",".join(group.bank_ids)))
    variances.sort(key=lambda variance: (variance.code, variance.event_date or date.min, variance.id))

    matched_bank_ids = {bank_id for group in groups for bank_id in group.bank_ids}
    expected_total = q2(sum((item.net_amount for item in ordered_expected), start=Decimal("0")))
    matched_bank_total = q2(sum((bank_by_id[bank_id].withdrawal_abs for bank_id in sorted(matched_bank_ids)), start=Decimal("0")))
    delta = q2(expected_total - matched_bank_total)

    has_blocker = any(item.severity == "blocker" and item.status == "open" for item in variances)
    has_review = any(item.severity == "review" and item.status == "open" for item in variances)
    if has_blocker:
        status = "Not tied"
    elif has_review:
        status = "Needs review"
    elif q2(abs(delta)) <= q2(policy.amount_tolerance):
        status = "Tied"
    else:
        status = "Not tied"

    summary = RunSummary(
        expected_net_pay=expected_total,
        matched_bank_total=matched_bank_total,
        delta=delta,
        status=status,
    )
    return ReconResult(match_groups=groups, variances=variances, summary=summary)
