#!/usr/bin/env bash
# Домен и бесплатный сертификат Let's Encrypt для сканера вилок.
#
#   sudo deploy/setup_domain.sh ваш-домен.ru ваша@почта
#
# Что делает скрипт (по шагам, каждый печатает результат):
#   1. ставит nginx и certbot, если их нет;
#   2. поднимает сайт по HTTP на вашем домене — этого хватает, чтобы
#      Let's Encrypt смог проверить, что домен действительно ваш;
#   3. выпускает сертификат (проверка файлом в /var/www/certbot, сайт при
#      этом не останавливается);
#   4. переключает nginx на HTTPS и уводит весь HTTP на него;
#   5. включает в приложении домен, secure-cookie и HSTS — записывает их
#      в /etc/arb-scanner.env, откуда их читает systemd-юнит;
#   6. проверяет автопродление (certbot продлевает сам, раз в сутки).
#
# Запускать можно повторно: сертификат переиспользуется, конфиг
# перезаписывается, ничего не дублируется.
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
ENV_FILE="${ENV_FILE:-/etc/arb-scanner.env}"
SERVICE="${SERVICE:-arb-scanner}"
WEBROOT=/var/www/certbot
SITE_NAME=arb-scanner
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "ОШИБКА: $*" >&2; exit 1; }
step() { echo; echo "== $* =="; }

[ -n "$DOMAIN" ] || die "укажите домен: sudo $0 ваш-домен.ru ваша@почта"
[ -n "$EMAIL" ] || die "укажите почту — на неё Let's Encrypt пришлёт
письмо, если сертификат вдруг перестанет продлеваться"
[ "$(id -u)" = 0 ] || die "нужны права root: запустите через sudo"
# Домен, а не URL и не IP: certbot откажется от «https://» в имени, а
# сообщение об этом невнятное.
[[ "$DOMAIN" =~ ^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]] \
  || die "«$DOMAIN» не похож на домен. Пишите без https:// и без пути,
например arb.example.ru"

step "Проверяю, куда указывает домен"
server_ip="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
domain_ip="$(getent ahostsv4 "$DOMAIN" | awk '{print $1; exit}' || true)"
echo "Адрес сервера: ${server_ip:-не определён}; домен смотрит на: ${domain_ip:-не определён}"
# Домена нет в DNS — дальше идти НЕЛЬЗЯ, и это не придирчивость. Проверку
# Let's Encrypt начинает с того, что сам спрашивает у DNS адрес домена:
# нет записи — нет и проверки, отказ гарантирован. А отказы считаются:
# лимит — пять неудачных проверок на имя в час, и, потратив их на заведомо
# провальные попытки, вы не сможете выпустить сертификат сразу после того,
# как запись наконец появится. Поэтому останавливаемся до обращения к
# Let's Encrypt, а не после.
if [ -z "$domain_ip" ]; then
  echo
  echo "Домена «$DOMAIN» в DNS нет вовсе (NXDOMAIN) — сертификат выпустить"
  echo "невозможно, и попытка только потратит лимит Let's Encrypt."
  echo
  echo "Что сделать:"
  echo "  1. в панели вашего DNS-провайдера создайте A-запись для"
  echo "     «$DOMAIN» со значением ${server_ip:-<адрес этого сервера>};"
  echo "  2. дождитесь, пока домен начнёт разрешаться — проверить можно"
  echo "     здесь же: getent hosts $DOMAIN (пусто — ещё не готово);"
  echo "  3. запустите эту же команду снова."
  echo
  echo "Если домен выдан сервисом динамического DNS, убедитесь, что сам"
  echo "сервис жив и имя за вами закреплено: бывает, что поддомен свободен"
  echo "лишь на вид, а родительская зона уже не отвечает."
  die "домен не разрешается — выпуск сертификата прерван"
fi
if [ -n "$server_ip" ] && [ "$server_ip" != "$domain_ip" ]; then
  # Здесь именно предупреждение: у сервера бывает несколько адресов
  # (IPv6, NAT), а свежая запись расходится по миру не мгновенно.
  echo "ВНИМАНИЕ: домен указывает не на этот сервер. Если A-запись только"
  echo "что изменена — подождите несколько минут. Пока DNS не обновится,"
  echo "Let's Encrypt не сможет проверить домен и выпуск не пройдёт."
fi

step "Ставлю nginx и certbot"
export DEBIAN_FRONTEND=noninteractive
need=()
command -v nginx >/dev/null || need+=(nginx)
command -v certbot >/dev/null || need+=(certbot)
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -qq
  apt-get install -y -qq "${need[@]}"
fi
echo "nginx: $(nginx -v 2>&1); certbot: $(certbot --version 2>&1)"

step "Поднимаю сайт по HTTP на домене $DOMAIN"
mkdir -p "$WEBROOT/.well-known/acme-challenge"
# Конфиг для проверки домена: сам сайт работает, а /.well-known отдаётся
# из каталога, куда certbot положит проверочный файл.
cat > "/etc/nginx/sites-available/$SITE_NAME" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN www.$DOMAIN;
    server_tokens off;

    location /.well-known/acme-challenge/ {
        root $WEBROOT;
        default_type "text/plain";
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
NGINX
ln -sf "/etc/nginx/sites-available/$SITE_NAME" \
       "/etc/nginx/sites-enabled/$SITE_NAME"
# Дефолтный сайт nginx перехватывает любой домен и мешает проверке.
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx || systemctl start nginx

step "Выпускаю сертификат Let's Encrypt"
# --webroot вместо --nginx: nginx не останавливается и не правится
# certbot'ом, конфигом дальше управляем мы сами (шаблон nginx-ssl.conf).
# deploy-hook перечитывает nginx после каждого продления.
issue() {
  certbot certonly --webroot -w "$WEBROOT" \
    --email "$EMAIL" --agree-tos --no-eff-email \
    --non-interactive --keep-until-expiring \
    --deploy-hook "systemctl reload nginx" "$@"
}
# www-поддомен есть далеко не всегда, а одно непроверяемое имя валит выпуск
# целиком. Поэтому сначала СПРАШИВАЕМ DNS, есть ли www: попытка «вслепую»
# стоит отказа, а отказы Let's Encrypt считает (пять на имя в час).
if [ -n "$(getent ahostsv4 "www.$DOMAIN" | awk '{print $1; exit}' || true)" ]; then
  if ! issue -d "$DOMAIN" -d "www.$DOMAIN"; then
    echo "С www не получилось — выпускаю сертификат только для $DOMAIN"
    issue -d "$DOMAIN"
  fi
else
  echo "A-записи www.$DOMAIN нет — выпускаю сертификат только для $DOMAIN"
  issue -d "$DOMAIN"
fi
[ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ] \
  || die "сертификат не появился в /etc/letsencrypt/live/$DOMAIN"

step "Переключаю nginx на HTTPS"
sed "s/__DOMAIN__/$DOMAIN/g" "$HERE/nginx-ssl.conf" \
  > "/etc/nginx/sites-available/$SITE_NAME"
# Сертификат без www — убираем www из server_name, иначе nginx будет
# обслуживать имя, которого нет в сертификате (браузер покажет ошибку).
if ! grep -q "www.$DOMAIN" "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" 2>/dev/null \
   && ! openssl x509 -in "/etc/letsencrypt/live/$DOMAIN/cert.pem" -noout -text \
        | grep -q "DNS:www.$DOMAIN"; then
  sed -i "s/ www\.$DOMAIN;/;/g" "/etc/nginx/sites-available/$SITE_NAME"
fi
nginx -t
systemctl reload nginx

step "Включаю домен и secure-cookie в приложении"
# Настройки живут в отдельном файле, а не в юните: обновление проекта
# перезапишет deploy/arb-scanner.service, а этот файл останется.
touch "$ENV_FILE"
chmod 640 "$ENV_FILE"
set_env() {
  local key="$1" value="$2"
  if grep -q "^$key=" "$ENV_FILE"; then
    sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    echo "$key=$value" >> "$ENV_FILE"
  fi
  echo "  $key=$value"
}
# SITE_DOMAIN: сервер отвечает только на свой Host — чужой домен,
# направленный на ваш IP, дальше шлюза не проходит.
set_env SITE_DOMAIN "$DOMAIN"
# COOKIE_SECURE=1: сессионная cookie уходит только по HTTPS.
set_env COOKIE_SECURE 1
# FORCE_HTTPS=1: открытый запрос, если он всё же дошёл до приложения,
# уводится на защищённый адрес.
set_env FORCE_HTTPS 1

if [ -f "/etc/systemd/system/$SERVICE.service" ]; then
  if ! grep -q "EnvironmentFile=-$ENV_FILE" "/etc/systemd/system/$SERVICE.service"; then
    echo "ВНИМАНИЕ: в /etc/systemd/system/$SERVICE.service нет строки"
    echo "  EnvironmentFile=-$ENV_FILE"
    echo "Обновите юнит из deploy/arb-scanner.service, иначе приложение не"
    echo "увидит эти настройки:"
    echo "  sudo cp deploy/arb-scanner.service /etc/systemd/system/"
    echo "  sudo systemctl daemon-reload && sudo systemctl restart $SERVICE"
  else
    systemctl restart "$SERVICE"
    echo "Служба $SERVICE перезапущена с новыми настройками."
  fi
else
  echo "Служба $SERVICE не установлена — запустите приложение с этими"
  echo "переменными сами (см. README, шаг «Автозапуск через systemd»)."
fi

step "Проверяю автопродление"
# Сертификат живёт 90 дней; пакет certbot приносит таймер, который
# продлевает его сам. Сухой прогон показывает, что продление пройдёт.
systemctl list-timers --all 2>/dev/null | grep -q certbot \
  && echo "Таймер продления certbot на месте." \
  || echo "ВНИМАНИЕ: таймера certbot не видно. Проверьте:
  systemctl enable --now certbot.timer"
certbot renew --dry-run --cert-name "$DOMAIN" >/dev/null 2>&1 \
  && echo "Пробное продление прошло — сертификат будет обновляться сам." \
  || echo "ВНИМАНИЕ: пробное продление не прошло. Посмотрите причину:
  certbot renew --dry-run"

echo
echo "Готово. Сайт: https://$DOMAIN"
echo "Проверьте вход: https://$DOMAIN/login — в адресной строке должен"
echo "быть замок, а HTTP-адрес должен сам переводить на HTTPS."
