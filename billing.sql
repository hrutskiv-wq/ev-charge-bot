DROP TABLE IF EXISTS kw_transactions CASCADE;
DROP TABLE IF EXISTS payments CASCADE;
DROP TYPE IF EXISTS transaction_type CASCADE;
DROP TYPE IF EXISTS payment_provider CASCADE;
DROP TYPE IF EXISTS payment_status CASCADE;

CREATE TYPE payment_status AS ENUM ('pending', 'success', 'failed', 'refunded');
CREATE TYPE payment_provider AS ENUM ('liqpay', 'monobank');
CREATE TYPE transaction_type AS ENUM ('deposit', 'withdrawal', 'bonus', 'correction');

CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    invoice_id VARCHAR(100) UNIQUE NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    provider payment_provider NOT NULL,
    status payment_status DEFAULT 'pending',
    payload JSONB,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE kw_transactions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    type transaction_type NOT NULL,
    amount NUMERIC(8, 2) NOT NULL,
    payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
    session_id INTEGER,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_payments_invoice ON payments(invoice_id);
CREATE INDEX idx_kw_transactions_user ON kw_transactions(user_id);
