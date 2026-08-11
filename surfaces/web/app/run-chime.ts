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
let refus: string | null = null;

function constructeur(): typeof AudioContext | undefined {
  if (typeof window === "undefined") return undefined;
  return (
    window.AudioContext
    || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  );
}

/**
 * Le navigateur refuse de démarrer un contexte audio hors d'un geste
 * utilisateur. On le crée donc à l'envoi du message — un clic ou une touche —
 * et non à la fin du run, où plus aucun geste ne le couvrirait.
 */
export function primeRunChime(): void {
  const Constructor = constructeur();
  if (!Constructor) {
    refus = "ce navigateur n'expose pas AudioContext";
    return;
  }
  if (!context) {
    try {
      context = new Constructor();
    } catch (erreur) {
      // Un contexte refusé n'est pas une panne : l'application marche sans son.
      refus = `création refusée (${(erreur as Error).name})`;
      context = null;
      return;
    }
  }
  if (context.state === "suspended") void context.resume();
}

const TIMBRES: Record<ChimeKind, { frequencies: number[]; gain: number }> = {
  // Deux notes montantes, brèves : « c'est fini », rien à faire.
  done: { frequencies: [660, 880], gain: 0.05 },
  // Trois notes plus graves et plus lentes : « on t'attend ».
  attention: { frequencies: [520, 415, 520], gain: 0.07 },
};

function emettre(actif: AudioContext, kind: ChimeKind): void {
  const { frequencies, gain } = TIMBRES[kind];
  // L'horloge est relue *après* la reprise : `resume()` est asynchrone, et une
  // note programmée sur un instant déjà passé pendant l'attente ne sonne
  // jamais. C'était la cause des runs muets — d'autant plus fréquente que le
  // run était court.
  const depart = actif.currentTime + 0.02;
  frequencies.forEach((frequency, index) => {
    const oscillateur = actif.createOscillator();
    const volume = actif.createGain();
    oscillateur.type = "sine";
    oscillateur.frequency.value = frequency;
    const debut = depart + index * 0.13;
    const fin = debut + 0.16;
    // Enveloppe douce : une onde coupée net produit un clic audible, plus
    // désagréable que la note elle-même.
    volume.gain.setValueAtTime(0.0001, debut);
    volume.gain.exponentialRampToValueAtTime(gain, debut + 0.02);
    volume.gain.exponentialRampToValueAtTime(0.0001, fin);
    oscillateur.connect(volume).connect(actif.destination);
    oscillateur.start(debut);
    oscillateur.stop(fin + 0.02);
  });
}

export function playRunChime(kind: ChimeKind = "done"): void {
  // Un contexte peut manquer si la fenêtre a été rechargée pendant un run :
  // aucun geste n'a alors préparé l'audio. On tente quand même — un navigateur
  // ayant déjà reçu une interaction l'autorise.
  if (!context) primeRunChime();
  const actif = context;
  if (!actif) {
    signalerUneFois(refus || "contexte audio indisponible");
    return;
  }
  if (actif.state === "running") {
    emettre(actif, kind);
    return;
  }
  // Suspendu : attendre la reprise avant de programmer quoi que ce soit.
  actif
    .resume()
    .then(() => emettre(actif, kind))
    .catch((erreur) => signalerUneFois(`reprise refusée (${(erreur as Error).name})`));
}

let signale = false;

/**
 * Un son absent ne doit pas rester inexplicable.
 *
 * Se taire renvoyait chercher au mauvais endroit — le réglage, le volume du
 * système — alors que la cause est une règle du navigateur. Une fois suffit :
 * répéter à chaque run remplacerait un silence par du bruit.
 */
function signalerUneFois(raison: string): void {
  if (signale) return;
  signale = true;
  console.warn(`AMK — signal de fin de run muet : ${raison}.`);
}
