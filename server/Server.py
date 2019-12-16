#!/usr/bin/python3

from concurrent import futures
import sys
sys.path.append('../')
from proto import ChatRoom_pb2_grpc as rpc
from proto import ChatRoom_pb2  as chat
import grpc
import time
from threading import Lock
from threading import Thread
from server import ChatRoom as room
import multiprocessing as mp
import os
from State import *
from FingerTable import *
from pysyncobj import *
from pysyncobj.batteries import ReplDict, ReplLockManager

# Obs  : ids : 29[11900] 1[11905] 5[11909]
# argv : 1 [numero de replicas], 2 [endereco do server 1], 3 [porta do server 1], 4 [endereco do server 2], 5 [porta do server 2], ...
class MainServer(SyncObj):
	def __init__(self):
		self.replica_address = []               # array to save replicad addresses (ip,port)

		print("Servidores replicas:")
		n = int(sys.argv[1]) - 1                # number of replicas
		i = 4                                   # controls the argv position
		while n > 0:
			self.replica_address.append((sys.argv[i],str(int(sys.argv[i+1]) + 1))) ## (ip,port,id)
			i = i + 2
			n = n - 1
		end = []                                 ## it will keep the string address
		for adr in self.replica_address:
			end.append(adr[0] + ':' + adr[1])## 'serverIP:serverPort'
			print(adr[0] + ':' + adr[1])

		super(MainServer, self).__init__(sys.argv[2] + ':' + str(int(sys.argv[3]) + 1),end) # self address + list of partners addresses #init replicas

		self.counter = 0
		self.address         = sys.argv[2]      # get ip of first server
		self.Request_port    = int(sys.argv[3]) # get port of first server
		self.route_table     = FingerTable(self.Request_port)
		self.ChatRooms	     = []	        ## List of Rooms will attach a  note
		self.lock	     = Lock()           ## Lock acess to critical regions
		self.id              = self.route_table.id
		self.state_file      = State_file(Lock(),self.route_table.id) 

		print("Server id : ",self.id,"(",self.Request_port,")")
		self.log_creation()


	def log_creation(self):
		try:
			self.recover_state()
		except:
			pass

		Thread(target=self.state_file.pop_log).start() # This thread will be responsible to write changes in the log file
		Thread(target=self.server_snapshot).start()    # This thread will be responsible to write the snapshots

		self.go_online()

	def go_online(self):
		server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
		rpc.add_ChatSServerServicer_to_server(ChatServer(self),server)
		print('Starting server, Listenning ...')
		server.add_insecure_port('[::]:' + str(self.Request_port))
		server.start()
		server.wait_for_termination()

		# Configure route table to deal with replicates and normal server (think if the table will be shared)
		if chatServer.id != 0:
			chatServer.route_table.add_node(2,11901)
			print("Send request")
			channel   = grpc.insecure_channel(chatServer.address + ':' + str(11912))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			conn.AddNewNode(chat.NewNodeReq(n_id=chatServer.id,port=chatServer.Request_port))


	@replicated
	def AddNewNode(self,request,context):
		others = self.route_table.add_node(request.n_id,request.port)
		return others

	def FindResponsible(self,request,context):
		resp_node = self.route_table.responsible_node(request.roomname)
		return resp_node

	# Como o pysyncobj não consegue lidar com objetos complexos (locks, estruturas requisicoes, etc...) foram criadas funcoes auxiliares que serão replicadas no lugar
	def CreateChat(self,roomname,password,nickname):
		if self.Validade_Room(roomname,password) == None:
			newroom = room.ChatRoom(roomname,password) # Chatroom receive
			newroom.Join(nickname)

			self.AuxCreateChat(newroom)

			self.state_file.stack_log('Created;' + nickname + ";" + roomname + ";" + password)

			return True
		else:
			return False

	@replicated
	def AuxCreateChat(self,newroom):
		self.ChatRooms.append(newroom)

	def JoinChat(self,request,context):
		room = self.Validade_Room_Index(request.roomname,request.password)
		if room < len(self.ChatRooms):
			if not self.ChatRooms[room].validate_user(request.nickname):
				print('JoinChat;' + request.nickname + ";" + request.roomname )
				self.AuxJoinChat(room,request.nickname)
				self.state_file.stack_log('JoinChat;' + request.nickname + ";" + request.roomname )

				return chat.JoinResponse(state = 'sucess',Port = 0)
		return chat.JoinResponse(state = 'fail',Port = 0)

	@replicated
	def AuxJoinChat(self,room,nickname):
		self.ChatRooms[room].Join(nickname)

	def ReceiveMessage(self,request,context):
		print("Rcv")
		lastindex = 0
		aux = self.Validade_User(request.roomname,request.nickname)
		if aux != None:
			while True:
				while lastindex < len(aux.Chats):
					n = aux.Chats[lastindex]
					n = chat.Note(roomname=request.roomname, nickname=n['nickname'], message=n['message'])
					lastindex+=1
					yield n

	def SendMessage(self,request,context):
		aux = self.Validade_User_Index(request.roomname,request.nickname)
		print(aux)
		if aux < len(self.ChatRooms):
			print('Message;' + request.nickname + ";" + request.roomname + ";" + request.message)
			self.AuxSendMessage(aux,request.nickname,request.roomname,request.message)
			self.state_file.stack_log('Message;' + request.nickname + ";" + request.roomname + ";" + request.message)

		return chat.EmptyResponse()

	@replicated
	def AuxSendMessage(self,room,nickname,roomname,message):
		self.ChatRooms[room].Chats.append({'nickname' : nickname,'message' : message})

	def Quit(self,request,context):
		aux = self.Validade_User(request.roomname,request.nickname)
		if aux != None:
			print('LeftChat;' + request.nickname + ";" + request.roomname )
			self.state_file.stack_log('LeftChat;' + request.nickname + ";" + request.roomname )
			aux.Chats.append({'nickname':request.nickname,'message' : request.nickname+' quited chat room;'})
			aux.Nicknames.remove(request.nickname)

			return chat.EmptyResponse()

	def Validade_User_Index(self,roomname,user):
		i   = 0
		self.lock.acquire()   ### multiple threas may acess this method at same time. though they cant do it currently
		for rooms in self.ChatRooms:
			if rooms.validate_name(roomname) and rooms.validate_user(user):
				break
			i += 1
		self.lock.release()
		return i

	def Validade_User(self,roomname,user):
		aux = None
		self.lock.acquire()   ### multiple threas may acess this method at same time. though they cant do it currently
		for rooms in self.ChatRooms:
			if rooms.validate_name(roomname) and rooms.validate_user(user):
				aux = rooms
		self.lock.release()
		return aux

	def Validade_Room_Index(self,Roomname,password):
		i   = 0
		self.lock.acquire()   ### multiple threas may acess this method at same time. though they cant do it currently
		for rooms in self.ChatRooms:
			if rooms.validate_name(Roomname) and rooms.validate_pass(password):
				break
			i += 1
		self.lock.release()
		return i
		

	def Validade_Room(self,Roomname,password):
		aux = None
		self.lock.acquire()   ### multiple threas may acess this method at same time. though they cant do it currently
		for rooms in self.ChatRooms:
			if rooms.validate_name(Roomname) and rooms.validate_pass(password):
				aux = rooms
		self.lock.release()
		return aux

	def room_identificator(self,roomname):
		result = hashlib.md5(roomname.encode())
		ident  = int(result.hexdigest(),16) % self.route_table.m

		return ident

	def getPort(self):
		return self.Request_port

	def server_snapshot(self):
		time.sleep(5)
		while True:
			print("Snapshot")
			aux   = []
			for i in self.ChatRooms:
				aux.append(i.to_dictionary())
			tm = time.time()
			state = {'time': tm,'server': aux}
			print("(",self.Request_port,")",state)

			self.state_file.take_snapshot(state)
			time.sleep(10)

	def recover_state(self):
		snap = self.state_file.read_snapshot()
		for r in snap['server']:
			newroom = room.ChatRoom(r['room'],r['password'],self.state_file.lock)
			for u in r['users']:
				newroom.Join(u)
			for m in r['mesgs']:
				newroom.Chats.append(m)
		self.ChatRooms.append(newroom)

		logs = self.state_file.read_log()
		for command in logs:
			if   command[0] == 'Created':
				newroom = room.ChatRoom(command[2],command[3],self.state_file.lock)
				newroom.Join(command[1])
				self.ChatRooms.append(newroom)
			elif command[0] == 'JoinChat':
				for ch in self.ChatRooms:
					if ch.Name == command[2]:
						ch.Join(command[1])
			elif command[0] == 'Message':
				for ch in self.ChatRooms:
					if ch.Name == command[2]:
						ch.Chats.append({'nickname' : command[1],'message' : command[3]})
			elif command[0] == 'LeftChat':
				for ch in self.ChatRooms:
					if ch.Name == command[2]:
						ch.Nicknames.remove(command[1])

		

class ChatServer(rpc.ChatSServerServicer):
	def __init__(self,server):
		self.server = server
 
	def AddNewNode(self,request,context):
		others = self.server.AddNewNode(request,context)
		for node in others:
			channel   = grpc.insecure_channel(self.server.address + ':' + str(node[1]))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			conn.AddNewNode(chat.NewNodeReq(n_id=request.n_id,port=request.port))

		return chat.EmptyResponse()

	def Request_port(self):
		return self.server.Request_port

	def FindResponsible(self,request,context):
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname # the name of the room
		resp_serv = resp_node[1][1]  # port of the sever that will/might know who handle

		if resp_node[0] :
			return chat.FindRResponse(port=resp_serv)
		channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
		return conn.FindResponsible(chat.FindRRequest(roomname=room_name))

	def CreateChat(self,request,context):
		# Fist - try to descover who will handle the request -----------------------------------------------------------------------
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname # the id of the room
		resp_serv = resp_node[1][1]  # port of the sever that will/might know who handle

		if not resp_node[0]: # Communicate with the server that might know who will respond the request
			channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			result    = conn.FindResponsible(chat.FindRRequest(roomname=room_name))
			resp_serv = result.port

		# If this server is the one supposed to handle -----------------------------------------------------------------------------
		if resp_serv == self.Request_port():
			print("I handle",request.roomname,request.password,request.nickname)
			result = self.server.CreateChat(request.roomname,request.password,request.nickname)
			if result :
				return chat.JoinResponse(state = 'sucess',Port = 0)
			else:
				return chat.JoinResponse(state = 'fail',Port = 0)

		# Server knows who will handle --------------------------------------------------------------------------------------------
		print("I know who will handle")
		print("is : ",resp_serv)
		channel = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn    = rpc.ChatSServerStub(channel)  ## connection with the responsible server
		result  = conn.CreateChat(chat.CreateChatRequest(roomname=request.roomname,password=request.password,nickname=request.nickname))
		print("Finish him")
		return result

	def JoinChat(self,request,context):
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname
		resp_serv = resp_node[1][1]

		if not resp_node[0]: # Communicate with the server that might know who will respond the request
			channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			result    = conn.FindResponsible(chat.FindRRequest(roomname=room_name))
			resp_serv = result.port

		if resp_serv == self.Request_port():
			return self.server.JoinChat(request,context)

		channel = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn    = rpc.ChatSServerStub(channel)  ## connection with the responsible server
		return conn.JoinChat(chat.JoinChatRequest(roomname=request.roomname,password=request.password,nickname=request.nickname))

	def ReceiveMessage(self,request,context):
		print("Send it all")
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname
		resp_serv = resp_node[1][1]

		if not resp_node[0]: # Communicate with the server that might know who will respond the request
			channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			result    = conn.FindResponsible(chat.FindRRequest(roomname=room_name))
			resp_serv = result.port

		if resp_serv == self.Request_port():
			lastindex = 0

			aux = None
			while not aux:
				aux = self.server.Validade_User(request.roomname,request.nickname)
			print("Sol ",request.roomname,request.nickname)
			print(aux)

			if aux != None:
				print("Room :",aux.Name)
				while True:
					while lastindex < len(aux.Chats):
						print("Send")
						n = aux.Chats[lastindex]
						n = chat.Note(roomname=request.roomname, nickname=n['nickname'], message=n['message'])
						lastindex+=1
						yield n
		print("What")

		channel = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn    = rpc.ChatSServerStub(channel)  ## connection with the server
		for note in conn.ReceiveMessage(chat.First(roomname=request.roomname,nickname=request.nickname)):
			yield note


	def SendMessage(self,request,context):
		print("Receive Message")
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname
		resp_serv = resp_node[1][1]

		if not resp_node[0]: # Communicate with the server that might know who will respond the request
			channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			result    = conn.FindResponsible(chat.FindRRequest(roomname=room_name))
			resp_serv = result.port

		if resp_serv == self.Request_port():
			print("I handle")
			return self.server.SendMessage(request,context)

		channel = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn    = rpc.ChatSServerStub(channel)  ## connection with the server
		return conn.SendMessage(chat.Note(roomname=request.roomname,nickname=request.nickname,message=request.message))


	def Quit(self,request,context):
		resp_node = self.server.FindResponsible(request,context)
		room_name = request.roomname
		resp_serv = resp_node[1][1]

		if not resp_node[0]: # Communicate with the server that might know who will respond the request
			channel   = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
			conn      = rpc.ChatSServerStub(channel)  ## connection with the responsible server
			result    = conn.FindResponsible(chat.FindRRequest(roomname=room_name))
			resp_serv = result.port

		if resp_serv == self.Request_port():
			return self.server.Quit(request,context)

		channel = grpc.insecure_channel(self.server.address + ':' + str(resp_serv))
		conn    = rpc.ChatSServerStub(channel)  ## connection with the server
		return conn.Quit(chat.QuitRequest(roomname=request.roomname,nickname=request.nickname))


if __name__ == '__main__':
	server = MainServer()
	print("Went")
	while True:
		time.sleep(0.25)
