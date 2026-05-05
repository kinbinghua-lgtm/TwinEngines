# -*- coding: utf-8 -*-
import json
from scipy.optimize import minimize
from math import exp
import pickle

def load_data():
    data = []
    with open('logs/shadow_signals.jsonl', 'r') as f:
        for line in f:
            try:
                obj = json.loads(line.strip())
                
                d = obj.get('d_abs_pct', 0)
                T = obj.get('t_remaining_sec', 0)
                
                poly = obj.get('polymarket', {})
                book = poly.get('book', {})
                
                ask = book.get('best_ask')
                bid = book.get('best_bid')
                
                if ask and bid and 0 < ask < 0.99:
                    data.append({
                        'd': float(d),
                        'T': float(T),
                        'ask': float(ask),
                        'reversal': obj.get('final_reversal', False),
                    })
            except:
                pass
    
    return data

def survival_formula(d, T, lambda0, gamma, beta):
    try:
        g = gamma + 1.0
        H = lambda0 * exp(beta * d) * (T ** g) / ((120 ** gamma) * g)
        H = min(H, 50)
        p = 1 - exp(-H)
        return max(0.001, min(0.999, p))
    except:
        return 0.5

def ev_loss(params, data):
    lambda0, gamma, beta = params
    
    total_ev = 0
    
    for window in data:
        d = window['d']
        T = window['T']
        ask = window['ask']
        reversal = window['reversal']
        
        p_rev = survival_formula(d, T, lambda0, gamma, beta)
        
        ev = p_rev * (1 - ask) - (1 - p_rev) * ask
        
        if ev > 0:
            if reversal:
                actual_ev = (1 - ask)
            else:
                actual_ev = -ask
        else:
            actual_ev = 0
        
        total_ev += actual_ev
    
    return -total_ev

data = load_data()
print('Sample count: %d' % len(data))

if len(data) < 5:
    print('Not enough data')
    exit(1)

result = minimize(
    ev_loss,
    x0=[0.1, 1.0, 0.0],
    args=(data,),
    method='L-BFGS-B',
    bounds=[
        (1e-6, 20.0),
        (0.1, 4.0),
        (-8.0, 8.0),
    ]
)

lambda0, gamma, beta = result.x

print('Training result:')
print('  lambda0: %.4f' % lambda0)
print('  gamma: %.4f' % gamma)
print('  beta: %.4f' % beta)
print('  total_ev: %.2f' % (-result.fun))

with open('ev_model_params.pkl', 'wb') as f:
    pickle.dump({
        'lambda0': float(lambda0),
        'gamma': float(gamma),
        'beta': float(beta),
        'total_ev': float(-result.fun),
    }, f)

print('Parameters saved')
