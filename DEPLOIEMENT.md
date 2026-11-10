# Mettre PC Monitor en ligne

## Comment ça marche
```
Visiteur ──► https://monitor.votredomaine.com   (page web statique, hébergée sur le VPS)
               │
               └─ la page lit http://127.0.0.1:8765 ──► PCMonitor-Agent.exe sur SON PC
```
- Chaque visiteur voit **son propre PC**. L'agent tourne chez lui, la page lui parle en local.
- **Aucune donnée ne passe par le VPS.** Il sert seulement la page et le fichier `.exe`.
- L'agent n'accepte que **votre domaine**. Les autres sites et les attaques par « DNS rebinding » sont bloqués.

## 1. Acheter le VPS et le domaine (vous-même)
| | Conseil | Prix indicatif |
|---|---|---|
| VPS | Hetzner CX22 ou OVH VPS Starter, **Ubuntu 24.04** | 4 à 6 €/mois |
| Domaine | OVH, Namecheap, Cloudflare… | environ 10 €/an |

Lors de la création du VPS, ajoutez votre **clé SSH publique**. Si vous n'en avez pas encore, lancez `ssh-keygen` dans PowerShell, puis copiez le contenu de `~\.ssh\id_ed25519.pub`.

## 2. Pointer le domaine vers le VPS
Chez votre registrar, ajoutez un enregistrement DNS :

| Type | Nom | Valeur |
|---|---|---|
| A | `monitor` | l'IP de votre VPS |

La propagation prend de quelques minutes à 1 heure. Pour vérifier : `nslookup monitor.votredomaine.com`.

## 3. Déployer (une seule commande, depuis ce dossier)
```powershell
.\deploy.ps1 -Domain monitor.votredomaine.com -Server IP_DU_VPS
```
Cette commande :
1. compile `PCMonitor-Agent.exe` pour **votre** domaine ;
2. envoie `site/` et `deploy/` sur le VPS, dans `/opt/pcmonitor` ;
3. installe Docker, le pare-feu (ports 22, 80 et 443) et les mises à jour de sécurité automatiques ;
4. démarre Caddy, qui obtient le certificat HTTPS tout seul.

Le site est ensuite disponible sur **https://monitor.votredomaine.com**.

Pour une **mise à jour** (nouvelle version de l'agent ou de la page), relancez la même commande.

## Fichiers
| Fichier | Rôle |
|---|---|
| `agent/server.py` | agent : collecte des métriques et API locale |
| `agent/cleaner.py` | nettoyage, RAM, gros fichiers, démarrage |
| `agent/dashboard.html` | interface, copiée dans `site/index.html` |
| `build_agent.bat` | compile l'agent `.exe` pour un domaine donné |
| `deploy/` | Caddy, Docker Compose et script d'installation du VPS |
| `deploy.ps1` | compile, envoie et installe |
| `Lancer_PC_Monitor.bat` | mode 100 % local, sans site |

## Bon à savoir
- **« Windows a protégé votre PC » (SmartScreen)** : ce message apparaît parce que le `.exe` n'est pas signé. Pour le supprimer, achetez un certificat de signature de code (environ 100 à 300 €/an, par exemple Certum ou SSL.com) et signez le fichier avec `signtool`. Sans cela, les visiteurs doivent cliquer sur *Informations complémentaires → Exécuter quand même*.
- **Les antivirus** signalent parfois les `.exe` créés avec PyInstaller. La signature réduit aussi ce problème.
- **Chrome et Edge** demandent au visiteur l'autorisation d'accéder au « réseau local » la première fois. Il doit cliquer sur **Autoriser**.
- **Navigateurs** : Chrome, Edge et Firefox fonctionnent. Les fonctions de l'agent sont réservées à Windows.
- **Droits administrateur** : certaines options de nettoyage système ne fonctionnent que si l'agent est lancé en administrateur (clic droit, puis « Exécuter en tant qu'administrateur »).
