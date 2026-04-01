//! Fast Polymarket order execution in Rust.
//! Called from Python via subprocess. ~200ms vs Python's ~1100ms.

use polymarket_hft::client::polymarket::clob::{
    TradingClient, ApiKeyCreds, Chain, OrderType, TickSize, UserLimitOrder, Side,
};
use alloy_signer_local::PrivateKeySigner;
use alloy_primitives::Address;
use serde::Serialize;
use std::env;
use std::time::Instant;

#[derive(Serialize)]
struct OrderResult {
    ok: bool,
    filled: bool,
    status: String,
    order_id: String,
    executed_size: f64,
    executed_price: f64,
    executed_notional: f64,
    elapsed_ms: u64,
    error: String,
}

#[tokio::main]
async fn main() {
    let start = Instant::now();
    let args: Vec<String> = env::args().collect();

    if args.len() < 9 {
        eprintln!("Usage: {} <private_key> <api_key> <api_secret> <api_passphrase> <token_id> <price> <size> <side> [FAK|GTC|FOK]", args[0]);
        let r = OrderResult {
            ok: false, filled: false, status: "error".into(), order_id: "".into(),
            executed_size: 0.0, executed_price: 0.0, executed_notional: 0.0,
            elapsed_ms: 0, error: "insufficient args".into(),
        };
        println!("{}", serde_json::to_string(&r).unwrap());
        return;
    }

    let pk_str = &args[1];
    let api_key = &args[2];
    let api_secret = &args[3];
    let api_passphrase = &args[4];
    let token_id = &args[5];
    let price: f64 = args[6].parse().unwrap_or(0.5);
    let size: f64 = args[7].parse().unwrap_or(10.0);
    let _side_str = &args[8]; // BUY or SELL
    let order_type_str = if args.len() > 9 { args[9].as_str() } else { "FAK" };

    let pk = if pk_str.starts_with("0x") { &pk_str[2..] } else { pk_str.as_str() };
    let wallet: PrivateKeySigner = match pk.parse() {
        Ok(w) => w,
        Err(e) => {
            let r = OrderResult {
                ok: false, filled: false, status: "key_error".into(), order_id: "".into(),
                executed_size: 0.0, executed_price: 0.0, executed_notional: 0.0,
                elapsed_ms: start.elapsed().as_millis() as u64, error: format!("{}", e),
            };
            println!("{}", serde_json::to_string(&r).unwrap());
            return;
        }
    };

    let creds = ApiKeyCreds {
        key: api_key.to_string(),
        secret: api_secret.to_string(),
        passphrase: api_passphrase.to_string(),
    };

    let client = TradingClient::new(wallet, creds, Chain::Polygon);

    let side = if _side_str.eq_ignore_ascii_case("SELL") { Side::Sell } else { Side::Buy };
    let order = UserLimitOrder {
        token_id: token_id.to_string(),
        price,
        size,
        side,
        fee_rate_bps: None,
        nonce: None,
        expiration: None,
        taker: Some(Address::ZERO),
    };

    let order_type = match order_type_str.to_uppercase().as_str() {
        "GTC" => OrderType::Gtc,
        "FOK" => OrderType::Fok,
        _ => OrderType::Fak,
    };

    let order_start = Instant::now();
    let resp = client
        .create_and_post_limit_order(&order, TickSize::PointZeroOne, false, order_type)
        .await;
    let order_ms = order_start.elapsed().as_millis() as u64;

    match resp {
        Ok(val) => {
            let status = val.get("status").and_then(serde_json::Value::as_str).unwrap_or("unknown").to_string();
            let oid = val.get("orderID").and_then(serde_json::Value::as_str).unwrap_or("").to_string();
            let filled = status.eq_ignore_ascii_case("matched");
            let r = OrderResult {
                ok: true, filled, status, order_id: oid,
                executed_size: if filled { size } else { 0.0 },
                executed_price: if filled { price } else { 0.0 },
                executed_notional: if filled { size * price } else { 0.0 },
                elapsed_ms: order_ms, error: "".into(),
            };
            println!("{}", serde_json::to_string(&r).unwrap());
        }
        Err(e) => {
            let r = OrderResult {
                ok: false, filled: false, status: "order_error".into(), order_id: "".into(),
                executed_size: 0.0, executed_price: 0.0, executed_notional: 0.0,
                elapsed_ms: order_ms, error: format!("{}", e),
            };
            println!("{}", serde_json::to_string(&r).unwrap());
        }
    }
}
