//! Fast Polymarket order execution in Rust (v2 — official EIP-712 signing).
//! Called from Python via subprocess. ~200ms vs Python's ~1100ms.
//!
//! Uses the same signing scheme as Polymarket/rs-clob-client:
//!   EIP-712 domain: "Polymarket CTF Exchange" v1, chain 137
//!   Neg-risk exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a
//!   L2 auth: HMAC-SHA256 with URL-safe base64

use alloy_primitives::{Address, U256};
use alloy_signer::SignerSync;
use alloy_signer_local::PrivateKeySigner;
use alloy_sol_types::{eip712_domain, SolStruct};
use base64::Engine;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use std::env;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

// Polygon mainnet exchange addresses
const NEG_RISK_EXCHANGE: &str = "C5d563A36AE78145C45a50134d48A1215220f80a";
const NORMAL_EXCHANGE: &str = "4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";

// EIP-712 Order struct — matches Polymarket CTF Exchange contract
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

// --- JSON types for API communication ---

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
    transaction_hashes: Vec<String>,
}

fn err_result(start: &Instant, status: &str, err: &str) -> OrderResult {
    OrderResult {
        ok: false,
        filled: false,
        status: status.to_string(),
        order_id: String::new(),
        executed_size: 0.0,
        executed_price: 0.0,
        executed_notional: 0.0,
        elapsed_ms: start.elapsed().as_millis() as u64,
        error: err.to_string(),
    }
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

#[tokio::main]
async fn main() {
    let start = Instant::now();
    let args: Vec<String> = env::args().collect();

    if args.len() < 9 {
        eprintln!(
            "Usage: {} <private_key> <api_key> <api_secret> <api_passphrase> \
             <token_id> <price> <size> <side> [FAK|GTC|FOK]",
            args[0]
        );
        println!(
            "{}",
            serde_json::to_string(&err_result(&start, "error", "insufficient args")).unwrap()
        );
        return;
    }

    let pk_str = &args[1];
    let api_key = &args[2];
    let api_secret = &args[3];
    let api_passphrase = &args[4];
    let token_id_str = &args[5];
    let price: f64 = args[6].parse().unwrap_or(0.5);
    let size: f64 = args[7].parse().unwrap_or(10.0);
    let side_str = &args[8];
    let order_type_str = if args.len() > 9 {
        args[9].as_str()
    } else {
        "FAK"
    };
    // Fee rate in basis points (taker fee, required for FAK/FOK orders)
    // Polymarket rejects fee_rate_bps=0 for taker orders
    let fee_rate_bps: u64 = if args.len() > 10 {
        args[10].parse().unwrap_or(0)
    } else {
        0
    };

    // Parse private key
    let pk = if pk_str.starts_with("0x") {
        &pk_str[2..]
    } else {
        pk_str.as_str()
    };
    let wallet: PrivateKeySigner = match pk.parse() {
        Ok(w) => w,
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(&start, "key_error", &format!("{}", e)))
                    .unwrap()
            );
            return;
        }
    };

    let signer_addr = wallet.address();

    // Parse token ID (decimal string -> U256)
    let token_id_u256: U256 = match token_id_str.parse() {
        Ok(v) => v,
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(
                    &start,
                    "token_error",
                    &format!("bad token_id: {}", e)
                ))
                .unwrap()
            );
            return;
        }
    };

    // Amount calculation — MUST match py_clob_client exactly:
    //   tick_size=0.01 -> RoundConfig(price=2, size=2, amount=4)
    //   price: round to 2 decimal places (normal rounding)
    //   size: round DOWN to 2 decimal places
    //   amount: price * size, max 4 decimal places, then * 1e6 for USDC
    //
    // Validate: price must have at most 2 decimal places
    let raw_price = (price * 100.0).round() / 100.0; // round_normal(price, 2)
    let raw_size = (size * 100.0).floor() / 100.0;   // round_down(size, 2)

    let is_buy = !side_str.eq_ignore_ascii_case("SELL");
    // Match py_clob_client's to_token_decimals: multiply by 1e6, then round to nearest int.
    // This avoids float precision issues (e.g. 0.54*9=4.8599... → 4860000 not 4859900)
    let (maker_amount, taker_amount) = if is_buy {
        // BUY: maker gives USDC (price*size*1e6), taker gives shares (size*1e6)
        let maker_usdc = (raw_price * raw_size * 1_000_000.0).round() as u64;
        let taker_shares = (raw_size * 1_000_000.0).round() as u64;
        (maker_usdc, taker_shares)
    } else {
        // SELL: maker gives shares (size*1e6), taker gives USDC (price*size*1e6)
        let taker_usdc = (raw_price * raw_size * 1_000_000.0).round() as u64;
        let maker_shares = (raw_size * 1_000_000.0).round() as u64;
        (maker_shares, taker_usdc)
    };

    // Generate salt (masked to 2^53-1 for JSON number safety)
    let salt: u64 = rand::random::<u64>() & ((1u64 << 53) - 1);

    // BTC Up/Down 5m = NOT neg-risk (neg_risk=false from API)
    // Use normal exchange address
    let neg_risk = if args.len() > 11 { args[11].as_str() == "true" } else { false };
    let exchange_addr: Address = if neg_risk {
        NEG_RISK_EXCHANGE.parse().unwrap()
    } else {
        NORMAL_EXCHANGE.parse().unwrap()
    };

    // Funder address (Proxy wallet): if set, maker=funder, signatureType=1(Proxy)
    // If not set, maker=signer, signatureType=0(EOA)
    let funder_str = if args.len() > 12 { args[12].as_str() } else { "" };
    let (maker_addr, sig_type) = if !funder_str.is_empty() && funder_str != "0" {
        let f: Address = funder_str.parse().unwrap_or(signer_addr);
        (f, 1u8)  // Proxy
    } else {
        (signer_addr, 0u8)  // EOA
    };

    // Build EIP-712 order
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
        feeRateBps: U256::from(fee_rate_bps),
        side: if is_buy { 0u8 } else { 1u8 },
        signatureType: sig_type, // 0=EOA, 1=Proxy
    };

    // Sign with EIP-712 domain
    let domain = eip712_domain! {
        name: "Polymarket CTF Exchange",
        version: "1",
        chain_id: CHAIN_ID,
        verifying_contract: exchange_addr,
    };
    const CHAIN_ID: u64 = 137;

    let sig_hash = order.eip712_signing_hash(&domain);
    let signature = match wallet.sign_hash_sync(&sig_hash) {
        Ok(s) => format!("0x{}", alloy_primitives::hex::encode(s.as_bytes())),
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(&start, "sign_error", &format!("{}", e)))
                    .unwrap()
            );
            return;
        }
    };

    // Build API JSON body
    let side_api = if is_buy { "BUY" } else { "SELL" };
    let order_type_api = match order_type_str.to_uppercase().as_str() {
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
            token_id: token_id_str.to_string(),
            maker_amount: maker_amount.to_string(),
            taker_amount: taker_amount.to_string(),
            expiration: "0".to_string(),
            nonce: "0".to_string(),
            fee_rate_bps: fee_rate_bps.to_string(),
            side: side_api.to_string(),
            signature_type: sig_type,
            signature: signature.clone(),
        },
        order_type: order_type_api.to_string(),
        owner: api_key.to_string(),
    };

    let body_json = match serde_json::to_string(&body) {
        Ok(j) => j,
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(&start, "json_error", &format!("{}", e)))
                    .unwrap()
            );
            return;
        }
    };

    // Build L2 HMAC auth headers
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let hmac_sig = match build_hmac_sig(api_secret, timestamp, "POST", "/order", &body_json) {
        Ok(s) => s,
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(&start, "hmac_error", &e)).unwrap()
            );
            return;
        }
    };

    // POST order
    let order_start = Instant::now();
    let http = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(10))
        .build()
    {
        Ok(c) => c,
        Err(e) => {
            println!(
                "{}",
                serde_json::to_string(&err_result(&start, "http_error", &format!("{}", e)))
                    .unwrap()
            );
            return;
        }
    };

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

    let order_ms = order_start.elapsed().as_millis() as u64;

    match resp {
        Ok(response) => {
            let status_code = response.status();
            let text = response.text().await.unwrap_or_default();

            if !status_code.is_success() {
                let r = OrderResult {
                    ok: false,
                    filled: false,
                    status: "api_error".to_string(),
                    order_id: String::new(),
                    executed_size: 0.0,
                    executed_price: 0.0,
                    executed_notional: 0.0,
                    elapsed_ms: order_ms,
                    error: format!("HTTP {}: {}", status_code, &text[..text.len().min(200)]),
                };
                println!("{}", serde_json::to_string(&r).unwrap());
                return;
            }

            // Parse response
            match serde_json::from_str::<ApiResponse>(&text) {
                Ok(api) => {
                    let filled = api.status.eq_ignore_ascii_case("matched");
                    let r = OrderResult {
                        ok: api.success || filled,
                        filled,
                        status: api.status,
                        order_id: api.order_id,
                        executed_size: if filled { size } else { 0.0 },
                        executed_price: if filled { price } else { 0.0 },
                        executed_notional: if filled { size * price } else { 0.0 },
                        elapsed_ms: order_ms,
                        error: api.error_msg.unwrap_or_default(),
                    };
                    println!("{}", serde_json::to_string(&r).unwrap());
                }
                Err(e) => {
                    let r = OrderResult {
                        ok: false,
                        filled: false,
                        status: "parse_error".to_string(),
                        order_id: String::new(),
                        executed_size: 0.0,
                        executed_price: 0.0,
                        executed_notional: 0.0,
                        elapsed_ms: order_ms,
                        error: format!("parse: {} body: {}", e, &text[..text.len().min(200)]),
                    };
                    println!("{}", serde_json::to_string(&r).unwrap());
                }
            }
        }
        Err(e) => {
            let r = OrderResult {
                ok: false,
                filled: false,
                status: "network_error".to_string(),
                order_id: String::new(),
                executed_size: 0.0,
                executed_price: 0.0,
                executed_notional: 0.0,
                elapsed_ms: order_ms,
                error: format!("{}", e),
            };
            println!("{}", serde_json::to_string(&r).unwrap());
        }
    }
}
