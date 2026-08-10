/**
 * SIGNAL SONORE DE FIN DE RUN
 * -----------------------------------------------------------------------------
 * Le son est synthétisé, pas chargé. Un fichier audio aurait imposé un binaire
 * au dépôt, une requête réseau et un cache à invalider, pour deux notes de
 * quelques centièmes de seconde.
 *
 * Deux timbres, parce que deux situations n'appellent pas la même réaction :
 * un run terminé est une information, un run qui attend une autorisation est une
 * demande. Les distinguer permet de savoir, sans regarder l'écran, s'il faut y
 * revenir tout de suite.
 */

type ChimeKind = "done" | "attention";

let context: AudioContext | null = null;

/**
 * Le navigateur refuse de démarrer un contexte audio hors d'un geste
 * utilisateur. On le crée donc à l'envoi du message — un clic ou une touche —
 * et non à la fin du run, où plus aucun geste ne le couvrirait.
 */
export function primeRunChime(): void {
  if (typeof window === "undefined") return;
  if (!context) {
    const Constructor =
      window.AudioContext
      || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Constructor) return;
    try {
      context = new Constructor();
    } catch {
      // Un contexte refusé n'est pas une panne : l'application marche sans son.
      context = null;
    }
  }
  // Un contexte créé avant le premier geste reste suspendu : le réveiller
  // pendant le clic, seul moment où le navigateur l'autorise sans discuter.
  if (context && context.state === "suspended") void context.resume();
}

const TIMBRES: Record<ChimeKind, { frequencies: number[]; gain: number }> = {
  // Deux notes montantes, brèves : « c'est fini », rien à faire.
  done: { frequencies: [660, 880], gain: 0.05 },
  // Trois notes plus graves et plus lentes : « on t'attend ».
  attention: { frequencies: [520, 415, 520], gain: 0.07 },
};

export function playRunChime(kind: ChimeKind = "done"): void {
  if (!context) return;
  if (context.state === "suspended") void context.resume();
  const { frequencies, gain } = TIMBRES[kind];
  const depart = context.currentTime;
  frequencies.forEach((frequency, index) => {
    const oscillateur = context!.createOscillator();
    const volume = context!.createGain();
    oscillateur.type = "sine";
    oscillateur.frequency.value = frequency;
    const debut = depart + index * 0.13;
    const fin = debut + 0.16;
    // Enveloppe douce : une onde coupée net produit un clic audible, plus
    // désagréable que la note elle-même.
    volume.gain.setValueAtTime(0.0001, debut);
    volume.gain.exponentialRampToValueAtTime(gain, debut + 0.02);
    volume.gain.exponentialRampToValueAtTime(0.0001, fin);
    oscillateur.connect(volume).connect(context!.destination);
    oscillateur.start(debut);
    oscillateur.stop(fin + 0.02);
  });
}
