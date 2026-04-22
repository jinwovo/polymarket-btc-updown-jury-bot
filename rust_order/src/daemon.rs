//! Polymarket order daemon — persistent process with TLS keep-alive.
//! Reads JSON order requests from stdin, writes JSON results to stdout.
//! Saves ~300-500ms per order by reusing TLS connection + skipping process startup.
//!
//! Usage: polymarket-order-daemon <private_key> <api_key> <api_secret> <api_passphrase>
//! Then send JSON lines on stdin:
//!   {"token_id":"...","price":0.5,"size":10,"side":"BUY","order_type":"FAK","fee_rate_bps":1000,"neg_risk":false,"funder":"0x..."}

use alloy_primitives::{Address, U256};
use alloy_signer::SignerSync;
use alloy_signer_local::PrivateKeySigner;
use alloy_sol_types::{eip712_domain, SolStruct};
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::env;
use std::io::{self, Write};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::time::timeout;

const NEG_RISK_EXCHANGE: &str = "C5d563A36AE78145C45a50134d48A1215220f80a";
const NORMAL_EXCHANGE: &str = "4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";
const CHAIN_ID: u64 = 137;

alloy_sol_types::sol! {
    #[derive(Debug)]
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        address taker;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint256 expiration;
        uint256 nonce;
        uint256 feeRateBps;
        uint8 side;
        uint8 signatureType;
    }
}

#[derive(Deserialize)]
struct OrderRequest {
    token_id: String,
    price: f64,
    size: f64,
    side: String,
    #[serde(default = "default_order_type")]
    order_type: String,
    #[serde(default)]
    fee_rate_bps: u64,
    #[serde(default)]
    neg_risk: bool,
    #[serde(default)]
    funder: String,
}

fn default_order_type() -> String {
    "FAK".to_string()
}

#[derive(Serialize)]
struct ApiOrder {
    salt: u64,
    maker: String,
    signer: String,
    taker: String,
    #[serde(rename = "tokenId")]
    token_id: String,
    #[serde(rename = "makerAmount")]
    maker_amount: String,
    #[serde(rename = "takerAmount")]
    taker_amount: String,
    expiration: String,
    nonce: String,
    #[serde(rename = "feeRateBps")]
    fee_rate_bps: String,
    side: String,
    #[serde(rename = "signatureType")]
    signature_type: u8,
    signature: String,
}

#[derive(Serialize)]
struct PostOrderBody {
    order: ApiOrder,
    #[serde(rename = "orderType")]
    order_type: String,
    owner: String,
}

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

#[derive(Deserialize)]
struct ApiResponse {
    #[serde(default)]
    success: bool,
    #[serde(default, rename = "orderID")]
    order_id: String,
    #[serde(default)]
    status: String,
    #[serde(default, rename = "errorMsg")]
    error_msg: Option<String>,
    #[serde(default, rename = "transactionsHashes")]
    _transaction_hashes: Vec<String>,
}

fn build_hmac_sig(secret: &str, timestamp: u64, method: &str, path: &str, body: &str) -> Result<String, String> {
    let b64 = base64::engine::general_purpose::URL_SAFE;
    let b64_nopad = base64::engine::general_purpose::URL_SAFE_NO_PAD;
    let decoded = b64
        .decode(secret)
        .or_else(|_| b64_nopad.decode(secret))
        .map_err(|e| format!("base64 decode: {}", e))?;
    let mut mac =
        Hmac::<Sha256>::new_from_slice(&decoded).map_err(|e| format!("hmac init: {}", e))?;
    let msg = format!("{}{}{}{}", timestamp, method, path, body);
    mac.update(msg.as_bytes());
    Ok(b64.encode(mac.finalize().into_bytes()))
}

fn addr_to_lower(addr: Address) -> String {
    format!("0x{}", alloy_primitives::hex::encode(addr.as_slice()))
}

fn err_result(elapsed_ms: u64, status: &str, err: &str) -> OrderResult {
    OrderResult {
        ok: false,
        filled: false,
        status: status.to_string(),
        order_id: String::new(),
        executed_size: 0.0,
        executed_price: 0.0,
        executed_notional: 0.0,
        elapsed_ms,
        error: err.to_string(),
    }
}

async fn execute_order(
    http: &reqwest::Client,
    wallet: &PrivateKeySigner,
    api_key: &str,
    api_secret: &str,
    api_passphrase: &str,
    req: &OrderRequest,
) -> OrderResult {
    let start = Instant::now();
    let signer_addr = wallet.address();

    // Parse token ID
    let token_id_u256: U256 = match req.token_id.parse() {
        Ok(v) => v,
        Err(e) => return err_result(0, "token_error", &format!("bad token_id: {}", e)),
    };

    // Amount calculation
    let raw_price = (req.price * 100.0).round() / 100.0;
    let raw_size = (req.size * 100.0).floor() / 100.0;
    let is_buy = !req.side.eq_ignore_ascii_case("SELL");

    let (maker_amount, taker_amount) = if is_buy {
        let maker_usdc = (raw_price * raw_size * 1_000_000.0).round() as u64;
        let taker_shares = (raw_size * 1_000_000.0).round() as u64;
        (maker_usdc, taker_shares)
    } else {
        let taker_usdc = (raw_price * raw_size * 1_000_000.0).round() as u64;
        let maker_shares = (raw_size * 1_000_000.0).round() as u64;
        (maker_shares, taker_usdc)
    };

    let salt: u64 = rand::random::<u64>() & ((1u64 << 53) - 1);

    let exchange_addr: Address = if req.neg_risk {
        NEG_RISK_EXCHANGE.parse().unwrap()
    } else {
        NORMAL_EXCHANGE.parse().unwrap()
    };

    let (maker_addr, sig_type) = if !req.funder.is_empty() && req.funder != "0" {
        let f: Address = req.funder.parse().unwrap_or(signer_addr);
        (f, 1u8)
    } else {
        (signer_addr, 0u8)
    };

    // Build & sign EIP-712 order
    let order = Order {
        salt: U256::from(salt),
        maker: maker_addr,
        signer: signer_addr,
        taker: Address::ZERO,
        tokenId: token_id_u256,
        makerAmount: U256::from(maker_amount),
        takerAmount: U256::from(taker_amount),
        expiration: U256::ZERO,
        nonce: U256::ZERO,
        feeRateBps: U256::from(req.fee_rate_bps),
        side: if is_buy { 0u8 } else { 1u8 },
        signatureType: sig_type,
    };

    let domain = eip712_domain! {
        name: "Polymarket CTF Exchange",
        version: "1",
        chain_id: CHAIN_ID,
        verifying_contract: exchange_addr,
    };

    let sig_hash = order.eip712_signing_hash(&domain);
    let signature = match wallet.sign_hash_sync(&sig_hash) {
        Ok(s) => format!("0x{}", alloy_primitives::hex::encode(s.as_bytes())),
        Err(e) => return err_result(start.elapsed().as_millis() as u64, "sign_error", &format!("{}", e)),
    };

    let side_api = if is_buy { "BUY" } else { "SELL" };
    let order_type_api = match req.order_type.to_uppercase().as_str() {
        "GTC" => "GTC",
        "FOK" => "FOK",
        _ => "FAK",
    };

    let body = PostOrderBody {
        order: ApiOrder {
            salt,
            maker: addr_to_lower(maker_addr),
            signer: addr_to_lower(signer_addr),
            taker: addr_to_lower(Address::ZERO),
            token_id: req.token_id.clone(),
            maker_amount: maker_amount.to_string(),
            taker_amount: taker_amount.to_string(),
            expiration: "0".to_string(),
            nonce: "0".to_string(),
            fee_rate_bps: req.fee_rate_bps.to_string(),
            side: side_api.to_string(),
            signature_type: sig_type,
            signature,
        },
        order_type: order_type_api.to_string(),
        owner: api_key.to_string(),
    };

    let body_json = match serde_json::to_string(&body) {
        Ok(j) => j,
        Err(e) => return err_result(start.elapsed().as_millis() as u64, "json_error", &format!("{}", e)),
    };

    // HMAC auth
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let hmac_sig = match build_hmac_sig(api_secret, timestamp, "POST", "/order", &body_json) {
        Ok(s) => s,
        Err(e) => return err_result(start.elapsed().as_millis() as u64, "hmac_error", &e),
    };

    // POST order (reuses TLS connection via http client pool)
    let resp = http
        .post("https://clob.polymarket.com/order")
        .header("Content-Type", "application/json")
        .header("POLY_ADDRESS", addr_to_lower(signer_addr))
        .header("POLY_API_KEY", api_key)
        .header("POLY_PASSPHRASE", api_passphrase)
        .header("POLY_SIGNATURE", &hmac_sig)
        .header("POLY_TIMESTAMP", timestamp.to_string())
        .body(body_json)
        .send()
        .await;

    let elapsed_ms = start.elapsed().as_millis() as u64;

    match resp {
        Ok(response) => {
            let status_code = response.status();
            let text = response.text().await.unwrap_or_default();

            if !status_code.is_success() {
                return OrderResult {
                    ok: false,
                    filled: false,
                    status: "api_error".to_string(),
                    order_id: String::new(),
                    executed_size: 0.0,
                    executed_price: 0.0,
                    executed_notional: 0.0,
                    elapsed_ms,
                    error: format!("HTTP {}: {}", status_code, &text[..text.len().min(200)]),
                };
            }

            match serde_json::from_str::<ApiResponse>(&text) {
                Ok(api) => {
                    let filled = api.status.eq_ignore_ascii_case("matched");
                    OrderResult {
                        ok: api.success || filled,
                        filled,
                        status: api.status,
                        order_id: api.order_id,
                        executed_size: if filled { req.size } else { 0.0 },
                        executed_price: if filled { req.price } else { 0.0 },
                        executed_notional: if filled { req.size * req.price } else { 0.0 },
                        elapsed_ms,
                        error: api.error_msg.unwrap_or_default(),
                    }
                }
                Err(e) => OrderResult {
                    ok: false,
                    filled: false,
                    status: "parse_error".to_string(),
                    order_id: String::new(),
                    executed_size: 0.0,
                    executed_price: 0.0,
                    executed_notional: 0.0,
                    elapsed_ms,
                    error: format!("parse: {} body: {}", e, &text[..text.len().min(200)]),
                },
            }
        }
        Err(e) => OrderResult {
            ok: false,
            filled: false,
            status: "network_error".to_string(),
            order_id: String::new(),
            executed_size: 0.0,
            executed_price: 0.0,
            executed_notional: 0.0,
            elapsed_ms,
            error: format!("{}", e),
        },
    }
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 5 {
        eprintln!("Usage: {} <private_key> <api_key> <api_secret> <api_passphrase>", args[0]);
        eprintln!("Then send JSON order requests on stdin, one per line.");
        std::process::exit(1);
    }

    let pk_str = &args[1];
    let api_key = &args[2];
    let api_secret = &args[3];
    let api_passphrase = &args[4];

    // Parse private key once
    let pk = if pk_str.starts_with("0x") { &pk_str[2..] } else { pk_str.as_str() };
    let wallet: PrivateKeySigner = match pk.parse() {
        Ok(w) => w,
        Err(e) => {
            eprintln!("{{\"error\":\"bad private key: {}\"}}", e);
            std::process::exit(1);
        }
    };

    // Create persistent HTTP client with connection pooling (TLS keep-alive)
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .pool_max_idle_per_host(2)
        .tcp_keepalive(std::time::Duration::from_secs(30))
        .build()
        .expect("failed to build HTTP client");

    // Warm up TLS connection
    let _ = http.get("https://clob.polymarket.com/time").send().await;

    // Signal ready
    println!("{{\"status\":\"ready\"}}");
    let _ = io::stdout().flush();

    // Read orders from stdin with TLS keep-alive ping every 60s idle
    let keep_alive_interval = Duration::from_secs(60);
    let stdin = tokio::io::BufReader::new(tokio::io::stdin());
    let mut lines = tokio::io::AsyncBufReadExt::lines(stdin);

    loop {
        // Wait for next line with timeout — if idle too long, ping to keep TLS alive
        let line = match timeout(keep_alive_interval, lines.next_line()).await {
            Ok(Ok(Some(line))) => line,
            Ok(Ok(None)) => break,       // stdin closed (EOF)
            Ok(Err(_)) => break,          // stdin read error
            Err(_) => {
                // Timeout — no order for 60s, ping to keep TLS connection warm
                let _ = http.get("https://clob.polymarket.com/time").send().await;
                continue;
            }
        };

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        let req: OrderRequest = match serde_json::from_str(trimmed) {
            Ok(r) => r,
            Err(e) => {
                let r = err_result(0, "parse_error", &format!("bad input: {}", e));
                println!("{}", serde_json::to_string(&r).unwrap());
                let _ = io::stdout().flush();
                continue;
            }
        };

        let result = execute_order(&http, &wallet, api_key, api_secret, api_passphrase, &req).await;
        println!("{}", serde_json::to_string(&result).unwrap());
        let _ = io::stdout().flush();
    }
}
