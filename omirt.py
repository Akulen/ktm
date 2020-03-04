from scipy.sparse import csr_matrix
from sklearn.metrics import roc_auc_score, log_loss as ll
from sklearn.model_selection import train_test_split
from eval_metrics import all_metrics
from itertools import combinations
from autograd import grad
import autograd.numpy as np
import argparse
import sys
import os.path
from scipy.sparse import load_npz
import glob
import pandas as pd
import yaml
from datetime import datetime
import json
import random
from tqdm import tqdm


SENSITIVE_ATTR = 'school_id'
THIS_GROUP = 13 # 42: AUC=0.8, 13 AUC=0.49
all_pairs = np.array(list(combinations(range(100), 2)))
EPS = 1e-15


def log_loss(y, pred):
    this_pred = np.clip(pred, EPS, 1 - EPS)
    return -(y * np.log(this_pred) + (1 - y) * np.log(1 - this_pred)).sum()

def sigmoid(x):
    return 1 / (1 + np.exp(-x))


class OMIRT:
    def __init__(self, n_users=10, n_items=5, d=3, gamma=1., gamma_v=0., n_iter=1000, df=None, fair=False):
        self.n_users = n_users
        self.n_items = n_items
        self.d = d
        self.GAMMA = gamma
        self.GAMMA_V = gamma_v
        self.LAMBDA = 0.0#1
        self.mu = 0.
        # self.w = np.random.random(n_users + n_items)
        # self.V = np.random.random((n_users + n_items, d))
        self.y_pred = []
        self.predictions = []
        self.w = np.random.random(n_users)
        self.item_bias = np.random.random(n_items)
        self.V = np.random.random((n_users, d))
        self.item_embed = np.random.random((n_items, d))
        # self.V2 = np.power(self.V, 2)
        self.n_iter = n_iter
        self.fair = fair

        attribute = np.array(df[SENSITIVE_ATTR])
        self.protected = np.argwhere(attribute == THIS_GROUP).reshape(-1)
        self.unprotected = np.argwhere(attribute != THIS_GROUP).reshape(-1)
        
    def load(self, folder):
        # Load mu
        if self.d == 0:
            w = np.load(os.path.join(folder, 'coef0.npy')).reshape(-1)
        else:
            w = np.load(os.path.join(folder, 'w.npy'))
            V = np.load(os.path.join(folder, 'V.npy'))
            self.V = V[:self.n_users]
            self.item_embed = V[self.n_users:]
        self.w = w[:self.n_users]
        self.item_bias = w[self.n_users:]
        print('w user', self.w.shape)
        print('w item', self.item_bias.shape)

    def full_fit(self, X, y):
        # pywFM and libFM
        print('full fit', X.shape, y.shape)
        
        for _ in range(500):
            if _ % 100 == 0:
                pred = self.predict(X)
                print('loss', ll(y, pred))
                print(self.loss(X, y, self.mu, self.w, self.V, self.item_bias, self.item_embed) / len(y), self.w.sum(), self.item_bias.sum())
            # self.mu -= self.GAMMA * grad(lambda mu: self.loss(X, y, mu, self.w, self.V))(self.mu)
            gradient = grad(lambda w: self.loss(X, y, self.mu, w, self.V, self.item_bias, self.item_embed))(self.w)
            # print('grad', gradient.shape)
            self.w -= self.GAMMA * gradient
            self.item_bias -= self.GAMMA * grad(lambda item_bias: self.loss(X, y, self.mu, self.w, self.V, item_bias, self.item_embed))(self.item_bias)
            
            if self.GAMMA_V:
                self.V -= self.GAMMA_V * grad(lambda V: self.loss(X, y, self.mu, self.w, V, self.item_bias, self.item_embed))(self.V)
                self.item_embed -= self.GAMMA_V * grad(lambda item_embed: self.loss(X, y, self.mu, self.w, self.V, self.item_bias, item_embed))(self.item_embed)
                
            # print(self.predict(X))

    def full_relaxed_fit(self, X, y):
        # pywFM and libFM
        print('full relaxed fit', X.shape, y.shape)

        c = 0
        for step in tqdm(range(self.n_iter)):
            if step % 500 == 0:
                pred = self.predict(X)
                print('score', self.relaxed_auc(X, y, self.mu, self.w, self.V, self.item_bias, self.item_embed), self.w.sum(), self.item_bias.sum(), self.item_bias[:5])
                print('auc', roc_auc_score(y, pred))
                print('auc_1', roc_auc_score(y[self.protected], pred[self.protected]))
                print('auc_0', roc_auc_score(y[self.unprotected], pred[self.unprotected]))
                print(c)

            if step > 0 and step % 50 == 0:
                # Actually on valid
                auc_1 = self.relaxed_auc(X[self.protected], y[self.protected], self.mu, self.w, self.V, self.item_bias, self.item_embed)
                auc_0 = self.relaxed_auc(X[self.unprotected], y[self.unprotected], self.mu, self.w, self.V, self.item_bias, self.item_embed)
                c += np.sign(auc_1 - auc_0) * 0.01
                c = np.clip(c, -1, 1)
            # self.mu -= self.GAMMA * grad(lambda mu: self.loss(X, y, mu, self.w, self.V))(self.mu)
            gradient = grad(lambda w: self.auc_loss(X, y, c, self.mu, w, self.V, self.item_bias, self.item_embed))(self.w)
            # print('grad', gradient.shape, gradient)
            self.w -= self.GAMMA * gradient
            self.item_bias -= self.GAMMA * grad(lambda item_bias: self.auc_loss(X, y, c, self.mu, self.w, self.V, item_bias, self.item_embed))(self.item_bias)
            if self.GAMMA_V:
                self.V -= self.GAMMA_V * grad(lambda V: self.auc_loss(X, y, c, self.mu, self.w, V, self.item_bias, self.item_embed))(self.V)
                self.item_embed -= self.GAMMA_V * grad(lambda item_embed: self.auc_loss(X, y, c, self.mu, self.w, self.V, self.item_bias, item_embed))(self.item_embed)
                
            # print(self.predict(X))
            
    def fit(self, X, y):
        # pywFM and libFM
        
        for _ in range(1):
            # print(self.loss(X, y, self.mu, self.w, self.V))
            # self.mu -= self.GAMMA * grad(lambda mu: self.loss(X, y, mu, self.w, self.V))(self.mu)
            gradient = grad(lambda w: self.loss(X, y, self.mu, w, self.V, self.item_bias, self.item_embed))(self.w)
            # print('grad', gradient.shape)
            self.w -= 1 * gradient
            self.GAMMA_V = 0.1 
            if self.GAMMA_V:
                self.V -= self.GAMMA_V * grad(lambda V: self.loss(X, y, self.mu, self.w, V, self.item_bias, self.item_embed))(self.V)
            # print(self.predict(X))
            
    def predict_logits(self, X, mu=None, w=None, V=None, item_bias=None, item_embed=None):
        if mu is None:
            mu = self.mu
            w = self.w
            V = self.V
            item_bias = self.item_bias
            item_embed = self.item_embed

        users = X[:, 0]
        items = X[:, 1]
            
        y_pred = mu + w[users] + item_bias[items]
        if self.d > 0:
            y_pred += np.sum(V[users] * item_embed[items], axis=1)
        return y_pred

    def predict(self, X, mu=None, w=None, V=None, item_bias=None, item_embed=None):
        if mu is None:
            mu = self.mu
            w = self.w
            V = self.V
            item_bias = self.item_bias
            item_embed = self.item_embed

        y_pred = self.predict_logits(X, mu, w, V, item_bias, item_embed)
        return sigmoid(y_pred)
    
    def update(self, X, y):
        s = len(X)
        self.y_pred = []
        for x, outcome in zip(X, y):
            pred = self.predict(x.reshape(-1, 2))
            # print('update', x, pred, outcome)
            self.y_pred.append(pred.item())
            self.fit(x.reshape(-1, 2), outcome)
            # print(self.w.sum(), self.item_embed.sum())
        print(roc_auc_score(y, self.y_pred))

    def loss(self, X, y, mu, w, V, bias, embed):
        pred = self.predict(X, mu, w, V, bias, embed)
        return log_loss(y, pred) + self.LAMBDA * (
            mu ** 2 + np.sum(w ** 2) +
            np.sum(bias ** 2) + np.sum(embed ** 2) +
            np.sum(V ** 2))

    def auc_loss(self, X, y, c, mu, w, V, bias, embed):
        X_1 = X[self.protected]
        y_1 = y[self.protected]
        X_0 = X[self.unprotected]
        y_0 = y[self.unprotected]
        auc = self.relaxed_auc(X, y, mu, w, V, bias, embed)
        auc_1 = self.relaxed_auc(X_1, y_1, mu, w, V, bias, embed)
        auc_0 = self.relaxed_auc(X_0, y_0, mu, w, V, bias, embed)
        return 100 - auc - auc_1 #self.fair * c * (auc_1 - auc_0) + self.LAMBDA * (mu ** 2 + np.sum(w ** 2) + np.sum(V ** 2) + np.sum(bias ** 2) + np.sum(embed ** 2))

    def relaxed_auc(self, X, y, mu, w, V, bias, embed):
        assert len(y) > 100
        batch = np.random.choice(len(y), 100)
        y_batch = y[batch]
        pred = self.predict_logits(X[batch], mu, w, V, bias, embed)
        auc = 0
        n = len(y)
        metabatch = np.random.choice(len(all_pairs), 100)
        ii = all_pairs[metabatch][:, 0]
        jj = all_pairs[metabatch][:, 1]
        auc = sigmoid((pred[ii] - pred[jj]) * (y_batch[ii] - y_batch[jj])).sum()
        return auc

    def save_results(self, model, y_test, test):
        iso_date = datetime.now().isoformat()
        self.predictions.append({
            'fold': 0,
            'pred': self.y_pred,
            'y': y_test.tolist()
        })
        saved_results = {
            'description': 'OMIRT',
            'predictions': self.predictions,
            'model': model  # Possibly add a checksum of the fold in the future
        }
        with open(os.path.join(folder, 'results-{}.json'.format(iso_date)), 'w') as f:
            json.dump(saved_results, f)
        all_metrics(saved_results, test)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run OMIRT')
    parser.add_argument('X_file', type=str, nargs='?', default='dummy')
    parser.add_argument('--d', type=int, nargs='?', default=20)
    parser.add_argument('--iter', type=int, nargs='?', default=1000)
    parser.add_argument('--lr', type=float, nargs='?', default=1.)
    parser.add_argument('--lr2', type=float, nargs='?', default=0.)
    parser.add_argument('--small', type=bool, nargs='?', const=True, default=False)
    parser.add_argument('--auc', type=bool, nargs='?', const=True, default=False)
    parser.add_argument('--fair', type=bool, nargs='?', const=True, default=False)
    options = parser.parse_args()
    print(vars(options))

    if options.X_file == 'dummy':
        ofm = OMIRT(n_users=10, n_items=5, d=3)
        df = pd.DataFrame.from_dict([
            {'user_id': 0, 'item_id': 0, 'correct': 0},
            {'user_id': 0, 'item_id': 1, 'correct': 1}
        ])
        print(df)
        X = np.array(df[['user_id', 'item_id']])
        y = np.array(df['correct'])
        print(X, y)
        # print(ofm.predict(X))
        ofm.fit(X, y)
        print(ofm.predict(X))
        sys.exit(0)
    
    X_file = options.X_file
    #y_file = X_file.replace('X', 'y').replace('npz', 'npy')
    folder = os.path.dirname(X_file)

    with open(os.path.join(folder, 'config.yml')) as f:
        config = yaml.load(f)
        print(config)

    df = pd.read_csv(X_file)
    print(df.head())
    #df = pd.read_csv(X_file)
    X = np.array(df[['user_id', 'item_id']])
    y = np.array(df['correct'])
    nb_samples = len(y)
    
    # Are folds fixed already?
    X_trains = {}
    y_trains = {}
    X_tests = {}
    y_tests = {}
    folds = glob.glob(os.path.join(folder, 'folds/50weak{}fold*.npy'.format(nb_samples)))
    if folds:
        for i, filename in enumerate(folds):
            i_test = np.load(filename)
            print('Fold', i, i_test.shape)
            i_train = list(set(range(nb_samples)) - set(i_test))

            X_trains[i] = X[i_train]
            y_trains[i] = y[i_train]
            if options.small:
                i_test = i_test[:5000]  # Try on 50 first test samples
            X_tests[i] = X[i_test]
            y_tests[i] = y[i_test]

            df_train = df.iloc[i_train]


    if X_trains:
        X_train, X_test, y_train, y_test = (X_trains[0], X_tests[0],
                                            y_trains[0], y_tests[0])
        print(X_train.shape, X_test.shape)
    else:
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2,
                                                            shuffle=False)

    n = X_train.shape[1]
    ofm = OMIRT(config['nb_users'], config['nb_items'], options.d,
                gamma=options.lr, gamma_v=options.lr2, n_iter=options.iter, df=df_train, fair=options.fair)
    if options.auc:
        ofm.full_relaxed_fit(X_train, y_train)
    else:
        ofm.full_fit(X_train, y_train)
    
    # ofm.load(folder)
    y_pred = ofm.predict(X_train)
    print('train auc', roc_auc_score(y_train, y_pred))

    y_pred = ofm.predict(X_test)
    ofm.y_pred = y_pred.tolist()  # Save for future use
    print(X_test[:5])
    print(y_test[:5])
    print(y_pred[:5])
    print('test auc', roc_auc_score(y_test, y_pred))
    
    indices = np.load(folds[0])
    test = df.iloc[indices]

    # ofm.update(X_test, y_test)
    if len(X_test) > 10000:
        ofm.save_results(vars(options), y_test, test)
