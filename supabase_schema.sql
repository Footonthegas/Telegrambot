create table if not exists public.telegram_profiles (
    chat_id text primary key,
    user_id text not null,
    year text,
    semester text,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.telegram_attendance_cache (
    chat_id text primary key,
    user_id text not null,
    year text,
    semester text,
    status text not null,
    fetched_at double precision not null,
    messages_json text not null,
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.telegram_runtime_control (
    control_key text primary key,
    service_enabled boolean not null default true,
    updated_at timestamptz not null default timezone('utc', now()),
    updated_by_chat_id text
);

insert into public.telegram_runtime_control (
    control_key,
    service_enabled,
    updated_at,
    updated_by_chat_id
)
values (
    'global',
    true,
    timezone('utc', now()),
    null
)
on conflict (control_key) do nothing;

create index if not exists idx_telegram_profiles_updated_at
    on public.telegram_profiles (updated_at desc);

create index if not exists idx_telegram_attendance_cache_updated_at
    on public.telegram_attendance_cache (updated_at desc);
