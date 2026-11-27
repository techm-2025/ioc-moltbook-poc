#!/usr/bin/env python3
"""Tests for solver.py: the rule-based path offline, plus the LLM path with --live.

    docker compose run --rm -T --no-deps --entrypoint python bridge tests/test_solver.py [--live]
    python3 bridge/tests/test_solver.py     # from v2/, if `requests` is installed (rules only)

Case 1 is the official example from https://www.moltbook.com/skill.md ("AI Verification
Challenges"); the rest follow the same obfuscation (alternating caps, scattered ] ^ [ / -
symbols, doubled letters, words broken by symbols or spaces). Add real challenges that failed
(see /data/verification_log.jsonl in the bridge-state volume) as new cases.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import solver  # noqa: E402

CASES = [
    # (challenge_text, expected answer)
    ("A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE, wH-aTs] ThE/ nEw^ SpE[eD?", 15.0),
    ("A] lO^bSt-Er S[wImS aT/ tW]eNn-Tyy mE^tE[rS aNd] SlO/wS bY^ fI[vE", 15.0),
    ("A lO]bStEr HaS^ tHi-RtY tWo/ cLa[WsS aNd] GaI^nS sE-vEn^ mOrE, hOw] MaNy/ ToTaL?", 39.0),
    ("ThE] lO^bS-tEr PuShEs/ wItH^ fOrTy[ nEwToNs] aNd/ lOsEs^ tWeL-vE, wHaT] iS/ tHe^ nEt?", 28.0),
    ("eA[cH] lO^bS-tEr cLaW/ eXeRtS^ sIx[ nEwToNs], aNd] tHeRe/ ArE^ fO-uR cLaWs, ToTaL] fOrCe?", 24.0),
    ("A] lO^bS-tEr TrAvElS/ sI^xT[y mEtErS] iN/ tH^rEe sEcOnDs, sPeEd] iF/ dIvIdEd^ eVeNlY?", 20.0),
    ("tW]eNtY^ fI[vE pLuS/ sE-vE^nT[eEn", 42.0),
    ("A lObStEr WeIgHs^ eI-gHtEeN] gRaMs AnD/ dRoPs^ tHr[EeE", 15.0),
    ("tW eN tY mEtErS^ tImEs] tH rEe", 60.0),
    ("ThE] sPeEd^ iS/ fIf-TeEn[ aNd] It/ iNcReAsEs^ bY] nInE", 24.0),
    ("A lObStEr sPlItS] nInE-tY sIx^ sHrImP/ bEtWeEn] tWeLvE fRiEnDs", 8.0),
    ("oNe] hUnD-rEd^ tWeNtY mInUs^ tHiR[tY", 90.0),
]

# Real challenges from live runs on 2026-11-27, each answer confirmed by Moltbook (/verify -> 200).
# (text, verified answer, rules can solve it). The rules miss ones that need physics knowledge
# ("impulse" = force x time, "three lobsters push") - that is why the LLM is the primary solver.
REAL_CASES = [
    ("ThE] Lo.oB-StEr^ ClAw] DeLiV/Ers~ TwEnTy{ FiV-e} NeWtOnS| AnD~ StRiiKeS^ FoR] TwO< SeCOnDs> , Um~ WhAt| Is] ThE^ ImPuLsE?", 50.0, False),
    ("A] lOoObSsTtEr ClA-w] FoR^cE Is SaId To Be T wEeN tYy FoOouR NeUwToNs, Um AnD ThReE LoB.stErS PuSh In ToTaL, HoW MuCh ToTaL FoR^cE Is ThIs?", 72.0, False),
    ("ThIs] lOoObB-stErr~ cLaW] fOrCe^ iS tHiRtY TwO] neOtOnS umm, AnD] dUrInG^ dOmInAnCe] fIgHtS~ tHeY mUlTiPlY/ bY FoUr] teRrItOrY{ tEsTs, WhAt] Is] tHe/ tOtAl~ fOrCe?", 128.0, True),
    ("A] LoO b-StEr' S] ClAw^ ApPlIeS/ TwEnTy FiVe NoOoToOnS, Um{ AnD} ThEn- GaAiInS| FiFtEeN NoOtOnS~ WhAt'S< ThE] ToTaL/ FoRcE?", 40.0, True),
    ("A] lO-bStEr ClAw^ eXr-tS] tWeNtY/ fIvE nooToNs~ aNd{ a] rIvAl C|lAw^ eXr-tS} fIfTeEn, hOw< mUcH> cOmBiNeD fOrCe? um", 40.0, True),
    ("A] Lo.oB sT-ErRr SwImS^ iN tHe/ tIdE aNd HaS cLaW FoRcE oF tHiRtY TwO NeWtOnS, It GrIpS~ aN oBjEcT FoUrTeEn TiMeS, WhAt Is ThE/ ToTaL] FoRcE^ (mUlTiPlY)?", 448.0, True),
    ("LoO]b-StEr ClAw ExErTs ThIrTy TwO^ NeWtOnS, AnD/ AnOtHeR ClAw ExErTs FoUrTeEn~ NeWtOnS; ToTaL FoR cE? Lo.o bsss umm lxobqstwer", 46.0, True),
    ("LoObB]sTtEeRr^ ClA]w-FoR/cE LoVooO]ciTy? ThIrTy T wO^ NeWtOnS] MuLtIpLiEd\\ By~ FoUr, HoW/ MaNy< NeWtOnS ToTaL, Ummm?", 128.0, True),
]


def _check(label: str, fn, text: str, expected: float) -> bool:
    got = fn(text)
    if isinstance(got, tuple):
        got = got[0]
    ok = got is not None and abs(got - expected) < 0.005
    print(f"{'PASS' if ok else 'FAIL'}  {label:5} expected {expected:>7.2f}  got {got!s:>7}  | {text[:62]}")
    return ok


def main() -> int:
    live = "--live" in sys.argv  # also run the LLM path (needs LITELLM_* env, e.g. in the bridge container)
    results = [_check("rules", solver.solve_rules, t, e) for t, e in CASES]
    results += [_check("rules", solver.solve_rules, t, e) for t, e, rules_ok in REAL_CASES if rules_ok]
    if live:
        results += [_check("llm", solver.solve_llm, t, e) for t, e in CASES]
        results += [_check("llm", solver.solve_llm, t, e) for t, e, _ in REAL_CASES]
    failed = results.count(False)
    print(f"\n{len(results) - failed}/{len(results)} passed" + ("" if live else "  (add --live to also test the LLM path)"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
