#!/usr/bin/env python

'''
events.py - subscribe to all events and print them to stdout
'''
import ESL

con = ESL.ESLconnection('192.168.1.157', '8021', 'ClueCon')

if con.connected():
    print("Connected to FreeSWITCH ESL")
    con.events('plain', 'all')
    while 1:
        e = con.recvEvent()
        if e:
            print(e.serialize())