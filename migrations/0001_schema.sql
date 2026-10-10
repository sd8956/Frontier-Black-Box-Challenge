-- 0001_schema.sql
--
-- Las cuatro tablas del libro y del registro de tokens.
--
-- Identificadores en ingles, como el resto del codigo. Los nombres que viajan
-- por el cable no se tocan: el serializador sigue respondiendo `cuenta_id` y
-- `cuentaId` aunque la columna se llame `account_id`.
--
-- La fila de `tokens` ES el cliente. No hay una tabla de clientes aparte: cada
-- token es un cliente con su propio estado, y nada de un cliente es legible ni
-- escribible desde otro. Por eso `client_id` viaja en toda consulta y toda
-- escritura, y es la primera columna de cada clave primaria.

begin;

create table if not exists tokens (
    id          bigint generated always as identity primary key,
    token_hash  text        not null,
    label       text        not null,
    created_at  timestamptz not null default now(),
    created_by  text        not null,
    expires_at  timestamptz not null,
    revoked_at  timestamptz,
    revoked_by  text,
    constraint tokens_token_hash_key unique (token_hash)
);

comment on table  tokens is 'Registro de credenciales. La fila es el cliente.';
comment on column tokens.token_hash is 'HMAC-SHA256 del token con la pimienta. El secreto no se guarda.';
comment on column tokens.label      is 'Para quien es. Sirve para saber a quien revocar.';
comment on column tokens.expires_at is 'Vigente es no revocado y expires_at en el futuro, en UTC.';

create table if not exists accounts (
    client_id  bigint not null references tokens (id) on delete cascade,
    account_id text   not null,
    balance    bigint not null default 0,
    name       text   not null,
    primary key (client_id, account_id),
    -- La plata no queda negativa. Un rechazo por saldo insuficiente no llega
    -- hasta aca: el dominio lo corta antes y no guarda nada. Esto es la red
    -- de abajo, para que un error de calculo no escriba saldo negativo.
    constraint accounts_balance_check check (balance >= 0)
);

comment on table  accounts is 'Saldos por cliente. La clave es el cliente mas el id: AC-0001 puede existir en dos clientes y no es la misma fila.';
comment on column accounts.name is 'Se guarda porque ALTA lo recibe. La unicidad de nombre NO esta confirmada.';

create table if not exists operations (
    client_id        bigint  not null,
    operation_id     text    not null,
    kind             text    not null,
    amount           bigint  not null,
    debited_account  text,
    credited_account text,
    annulled         boolean not null default false,
    primary key (client_id, operation_id),
    constraint operations_client_fk
        foreign key (client_id) references tokens (id) on delete cascade,
    constraint operations_debited_fk
        foreign key (client_id, debited_account)  references accounts (client_id, account_id) on delete cascade,
    constraint operations_credited_fk
        foreign key (client_id, credited_account) references accounts (client_id, account_id) on delete cascade,
    -- Lo que guarda una operacion es lo necesario para deshacerla: con el
    -- monto, las dos cuentas y la bandera, ANULA puede revertir sin adivinar.
    constraint operations_shape_check check (
        (debited_account is not null or credited_account is not null)
    )
);

comment on table  operations is 'Un deposito llena solo la acreditada. Una transferencia llena las dos. Lo que falla no se guarda.';
comment on column operations.kind is 'deposit o transfer. No es una clave: no esta confirmado que el original no tenga mas tipos.';

create table if not exists counters (
    client_id      bigint  primary key references tokens (id) on delete cascade,
    next_account   integer not null default 1,
    next_operation integer not null default 1,
    constraint counters_positive_check check (next_account >= 1 and next_operation >= 1)
);

comment on table  counters is 'Una fila por cliente. NO es una secuencia global: el contador es por cliente.';

-- El candado es por cliente, no global, y el token se relee dentro de la
-- transaccion. Este indice sirve a la busqueda del registro, que ocurre en
-- cada request.
create index if not exists operations_by_client on operations (client_id);

-- RLS activo en las cuatro, sin politicas para anon. El proceso entra con el
-- rol de servidor, que no pasa por RLS; esto cierra la puerta a la Data API,
-- que si entra como anon. No es defensa contra nosotros, es contra el atajo.
alter table tokens     enable row level security;
alter table accounts   enable row level security;
alter table operations enable row level security;
alter table counters   enable row level security;

commit;
