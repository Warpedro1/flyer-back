-- Migration: Create plans table and seed initial data
-- Run this in the Supabase SQL Editor (https://supabase.com/dashboard → SQL Editor)

CREATE TABLE IF NOT EXISTS plans (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    name        text NOT NULL,
    price       numeric(10,2),          -- NULL = free
    interval    text,                    -- e.g. 'mês', 'ano'
    features    jsonb DEFAULT '[]'::jsonb,
    is_active   boolean DEFAULT true,
    created_at  timestamptz DEFAULT now()
);

-- Enable RLS (required by Supabase)
ALTER TABLE plans ENABLE ROW LEVEL SECURITY;

-- Public read access (plans are visible to everyone, including anon)
CREATE POLICY "Plans are publicly readable"
    ON plans FOR SELECT
    USING (true);

-- Only service_role can insert/update/delete (admin only)
CREATE POLICY "Only service_role can modify plans"
    ON plans FOR ALL
    USING (auth.role() = 'service_role');

-- Seed three starter plans
INSERT INTO plans (name, price, interval, features) VALUES
(
    'Grátis',
    NULL,
    NULL,
    '["Descobrir eventos próximos", "Check-in básico", "Chat com IA (limite diário)"]'::jsonb
),
(
    'Pro',
    9.99,
    'mês',
    '["Tudo do Grátis", "Recomendações personalizadas", "Modo social", "Check-ins ilimitados", "Sem anúncios"]'::jsonb
),
(
    'Enterprise',
    29.99,
    'mês',
    '["Tudo do Pro", "Criação ilimitada de eventos", "Eventos em destaque (boost)", "Suporte prioritário", "Relatórios e analytics"]'::jsonb
);
