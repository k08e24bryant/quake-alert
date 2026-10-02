# syntax=docker/dockerfile:1
# PostgreSQL 16 + PostGIS 3 for linux/arm64 (Oracle Ampere) and linux/amd64.
#
# postgis/postgis publishes amd64 only, so this builds the same thing from the official
# postgres image, whose apt sources already include apt.postgresql.org (PGDG), where the
# PostGIS packages come from. CI builds it for both architectures and pushes it to GHCR.
# The postgis extension itself is created by our first Alembic migration.
FROM postgres:16-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "postgresql-${PG_MAJOR}-postgis-3" \
        "postgresql-${PG_MAJOR}-postgis-3-scripts" \
    && rm -rf /var/lib/apt/lists/*
