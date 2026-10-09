"""
Référentiel des gares TrainNomad — lance les opérateurs l'un après l'autre, puis la fusion.

Un seul script tourne à la fois (ils partagent le même cache). Chaque opérateur est incrémental :
une gare déjà en base n'est pas recherchée de nouveau, relancer ne coûte donc presque rien. Si un
opérateur échoue (réseau coupé...), les suivants sont lancés quand même et la fusion se fait avec
ce qui existe ; il suffit de relancer pour reprendre là où il s'est arrêté.

Usage :
    python referentiel/build_tout.py                              tous les opérateurs, puis la fusion
    python referentiel/build_tout.py italo eurostar uk            seulement ceux-là, dans cet ordre
    python referentiel/build_tout.py trenitalia --avec-overpass   les options sont transmises à chaque script
"""
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# du plus court au plus long
OPERATEURS = ["eurostar", "european_sleeper", "flixtrain", "ouigo_es", "italo", "sncb", "cp", "renfe", "sncf", "swiss",
              "trenitalia", "uk"]


def main():
    args = sys.argv[1:]
    options, i = [], 0
    choisis = []
    while i < len(args):
        if args[i].startswith("--"):
            options.append(args[i])
            if args[i] == "--limit" and i + 1 < len(args):
                i += 1
                options.append(args[i])
        else:
            choisis.append(args[i].lower())
        i += 1
    inconnus = [o for o in choisis if o not in OPERATEURS]
    if inconnus:
        sys.exit(f"❌ opérateur inconnu : {', '.join(inconnus)} (connus : {', '.join(OPERATEURS)})")

    bilan = []
    for op in choisis or OPERATEURS:
        t0 = time.time()
        code = subprocess.run([sys.executable, os.path.join(BASE_DIR, f"build_gares_{op}.py"), *options]).returncode
        bilan.append((op, code, time.time() - t0))
    code_fusion = subprocess.run([sys.executable, os.path.join(BASE_DIR, "fusion.py")]).returncode

    print("\n" + "=" * 60)
    for op, code, duree in bilan:
        print(f"{'✅' if code == 0 else '❌'} {op:<18} {duree / 60:6.1f} min")
    print(f"{'✅' if code_fusion == 0 else '❌'} fusion")
    if any(code for _, code, _ in bilan):
        print("➡️  Relancer la même commande pour reprendre les opérateurs en échec.")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
