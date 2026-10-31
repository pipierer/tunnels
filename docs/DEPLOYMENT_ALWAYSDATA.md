# Déploiement sur Alwaysdata Free

Ce document décrit la création de la base MySQL sur Alwaysdata. **C'est la seule chose à faire sur Alwaysdata** — tout le reste (site, API, WS) est sur Render.

## 1. Créer un compte Alwaysdata

1. Aller sur https://www.alwaysdata.com
2. Cliquer **S'inscrire**.
3. Choisir l'offre **Free** (gratuite, 100 Mo, 1 site, 1 base MySQL).
4. Remplir le formulaire : nom d'utilisateur = votre compte (ex. `moncompte`), email, mot de passe.
5. Valider. Vous recevez un email de confirmation.

## 2. Créer la base de données MySQL

1. Se connecter au panel : https://admin.alwaysdata.com
2. Menu **Bases de données** → **Bases de données** → **Ajouter une base de données**.
3. Remplir :
   - **Nom** : `bentunnel` (le nom complet sera `moncompte_bentunnel`)
   - **Type** : MySQL/MariaDB
   - **Utilisateur** : créer un nouvel utilisateur (ex. `bentunnel_user`) avec un mot de passe fort
4. Noter précieusement :
   - **Hostname** : `mysql-moncompte.alwaysdata.net`
   - **Port** : `3306`
   - **Database** : `moncompte_bentunnel`
   - **User** : `moncompte_bentunnel_user` (Alwaysdata préfixe par le compte)
   - **Password** : celui que vous venez de définir

## 3. Autoriser Render à se connecter à distance

Par défaut, Alwaysdata autorise les connexions MySQL distantes, mais il faut s'assurer que l'utilisateur MySQL accepte les connexions depuis n'importe quelle IP (Render a des IPs dynamiques).

1. Panel Alwaysdata → **Bases de données** → **Utilisateurs MySQL**.
2. Éditer l'utilisateur `bentunnel_user`.
3. Dans le champ **Hôte** (ou **Client**), mettre `%` (joker = toutes les IPs).
   - Alternative plus restrictive : mettre l'IP de Render si elle est fixe, mais Render Free a des IPs dynamiques → `%` est nécessaire.
4. Sauvegarder.

> **Note sécurité** : `%` est large. Alwaysdata chiffre les mots de passe MySQL et le tunnel MySQL est en TLS par défaut. Le risque est acceptable pour une DB de tunnels. Si vous voulez verrouiller davantage, ajoutez un pare-feu MySQL au niveau utilisateur Alwaysdata en whitelistant les plages d'IP Render (cf. https://render.com/docs/static-outbound-ip-addresses — non garanti stable sur Free).

## 4. Tester la connexion depuis votre PC (optionnel)

```bash
mysql -h mysql-moncompte.alwaysdata.net -P 3306 -u moncompte_bentunnel_user -p moncompte_bentunnel
```

Si la connexion fonctionne, l'accès distant est OK.

## 5. Construire l'URL de connexion pour Render

Au format SQLAlchemy asyncmy :

```
mysql+asyncmy://USER:PASSWORD@HOST:PORT/DBNAME?charset=utf8mb4
```

Exemple :
```
mysql+asyncmy://moncompte_bentunnel_user:MonMdpFort123@mysql-moncompte.alwaysdata.net:3306/moncompte_bentunnel?charset=utf8mb4
```

> **Important** : URL-encoder le mot de passe s'il contient des caractères spéciaux (`@`, `:`, `/`, `?`, `#`, `&`). En Python : `import urllib.parse; urllib.parse.quote("p@ss:w/ord")` → `p%40ss%3Aw%2Ford`.

## 6. C'est tout côté Alwaysdata

Le schéma de tables (`users`, `tunnels`, `sessions`, `revoked_tokens`, `audit_logs`) est **créé automatiquement** par le serveur Render au premier démarrage via `init_db()` (qui appelle `Base.metadata.create_all`). Vous n'avez **pas** à exécuter de SQL manuellement.

Pour vérifier que les tables sont bien créées après le déploiement Render, vous pouvez utiliser phpMyAdmin Alwaysdata :
1. Panel → **Bases de données** → cliquer sur **phpMyAdmin** à côté de votre base.
2. Vérifier que les 5 tables sont présentes.

## FAQ

**Q : L'offre Free a 100 Mo, c'est suffisant ?**
R : Oui. Un user BenTunnel occupe ~1 Ko, une session ~500 octets. Vous pouvez stocker ~100 000 utilisateurs dans 100 Mo.

**Q : Alwaysdata peut-il bloquer l'accès distant MySQL ?**
R : Non, c'est une fonctionnalité supportée sur tous les plans. Si vous rencontrez une erreur `Host 'xxx' is not allowed to connect`, vérifiez le champ **Hôte** de l'utilisateur MySQL (doit être `%`).

**Q : Le TLS est-il obligatoire entre Render et Alwaysdata ?**
R : Recommandé mais pas obligatoire. Pour l'activer, ajouter `ssl=true` à l'URL DATABASE_URL : `...?charset=utf8mb4&ssl=true`. Toujours vérifier que la connexion fonctionne après.

**Q : Que faire si je dépasse 40 connexions simultanées ?**
R : Le pool Render est configuré avec `pool_size=5, max_overflow=10` = 15 max. Vous êtes très loin de 40. Si cela arrive tout de même, le serveur Render log l'erreur et les requêtes en attente échouent temporairement.
