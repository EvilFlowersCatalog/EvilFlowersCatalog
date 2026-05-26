CREATE USER dataverse WITH PASSWORD 'secret';
CREATE DATABASE dataverse OWNER dataverse;
GRANT ALL PRIVILEGES ON DATABASE dataverse TO dataverse;
