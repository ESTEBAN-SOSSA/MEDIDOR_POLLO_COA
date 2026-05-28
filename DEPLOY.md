# Despliegue en Producción — MEDIDOR POLLO COA

## Servidor
- VPS Contabo: `217.77.10.85`
- Stack Portainer: `pollo-coa`

## Acceso API
```
GET http://217.77.10.85:8010/health
GET http://217.77.10.85:8010/api/v1/...
Header: X-API-Key: EDEMCO_2026_GROWAT_GENERACION
```

## Imágenes Docker
- API:     `pollo-coa-medidor-api:latest`
- Scraper: `pollo-coa-medidor-scraper:latest`
- DB:      `postgres:16-alpine`

## Variables de entorno requeridas
```
GROWATT_USER=<usuario growatt>
GROWATT_PASSWORD=<contraseña growatt>
GROWATT_LOGIN_URL=https://oss.growatt.com/login?lang=en
DATABASE_URL=postgresql+psycopg://...
API_KEY=EDEMCO_2026_GROWAT_GENERACION
TARGET_PLANT_IDS=1878757
TZ=America/Bogota
```

## Arquitectura completa
Ver [INFRASTRUCTURE.md](../ARCHITECTURE.md) en el repo EDEMCO-X.
